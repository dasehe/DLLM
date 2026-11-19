# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Planificador: decide qué nodos usar y qué parte del modelo da a cada uno.

Modelo de coste (decodificación, un solo stream, pipeline por capas):
    t_token ≈ Σ bytes_i / bw_i  +  Σ rtt_i
La inferencia es memory-bound, así que cada nodo tarda (bytes que guarda)/(su ancho de
banda de memoria), y los nodos se recorren en serie. Minimizar Σ bytes_i/bw_i con
Σ bytes_i = M y bytes_i ≤ cap_i se resuelve llenando los nodos MÁS RÁPIDOS primero,
hasta su capacidad, y usando el mínimo de nodos (cada nodo extra añade un salto de red).
Es una estimación gruesa: sirve para ordenar y para avisar, no como benchmark.

Topología de red (0.5.0): cada nodo tiene un TIER según su enlace con el orquestador (ver `classify`). Entre los
repartos que caben, se elige por este orden: (1) el tier más alto usado, lo más bajo posible, (2) menos saltos de
red (nodos remotos), (3) menor tiempo por token estimado, (4) menos nodos. Un nodo de tier 3 solo entra si el
modelo no cabe sin él.
"""
from itertools import combinations

GiB = 1 << 30
OVERHEAD_NODE = 512 << 20  # buffers de cómputo / contexto CUDA por nodo
COMPUTE_FRAC = 0.05        # margen para buffers de cómputo del modelo
HEADROOM = 0.95            # no llenar el nodo al 100 %
EFFICIENCY = 0.65          # llama.cpp rinde ~60-70 % del ancho de banda teórico
HOST_MS = 3.0              # coste fijo por token en el orquestador

# Tiers de red (medidos por el orquestador con la sonda del túnel autenticado):
#   0 mismo equipo · 1 LAN rápida (≥ TIER1_MBPS y RTT < 1 ms) · 2 LAN normal (RTT ≤ TIER3_RTT_MS)
#   3 Wi-Fi / VPN / WAN (RTT > TIER3_RTT_MS). Lo que queda entre 1 y 10 ms cuenta como tier 2.
TIER1_MBPS = 2000.0
TIER1_RTT_MS = 1.0
TIER3_RTT_MS = 10.0
TIER_NAMES = {0: "local", 1: "LAN rápida", 2: "LAN", 3: "Wi-Fi/VPN/WAN"}
IO_RAM = 256 << 20        # RAM de buffers de E/S que reserva un slot en un nodo con GPU
MAX_SUBSET_NODES = 12      # búsqueda exhaustiva de subconjuntos hasta este número de nodos (2^12)


# bytes por elemento de cada tipo de caché KV de llama.cpp (los cuantizados incluyen su escala por bloque de 32)
KV_TYPE_BYTES = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
                 "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32}


UBATCH = 512  # llama.cpp guarda en las capas SWA la ventana + un lote


def swa_cells(ctx, n_swa):
    """Celdas de la caché de una capa SWA, como llama.cpp: min(ctx, ventana + lote), redondeado a 256."""
    c = min(ctx, n_swa + UBATCH)
    return -(-c // 256) * 256 if c < ctx else ctx


def kv_bytes(m, ctx, k_type="f16", v_type="f16"):
    """Caché KV: Σ por capa con caché propia de celdas · n_head_kv · (dim_k · bytes K + dim_v · bytes V).
    Las capas de ventana deslizante solo guardan la ventana (no todo el contexto)."""
    bk, bv = KV_TYPE_BYTES.get(k_type or "f16", 2.0), KV_TYPE_BYTES.get(v_type or "f16", 2.0)
    layers = m.get("kv_layers")
    try:
        if layers:
            n_swa = m.get("n_swa") or 0
            tot = 0
            for h, dk, dv, swa in layers:
                cells = swa_cells(ctx, n_swa) if swa and n_swa else ctx
                tot += cells * h * (dk * bk + dv * bv)
            return int(tot)
        head_dim = m["n_embd"] // m["n_head"]
        return int(m["n_layers"] * ctx * m["n_head_kv"] * head_dim * (bk + bv))
    except (TypeError, ZeroDivisionError, KeyError):
        return int(m["size"] * 0.1)  # sin metadatos: estimación conservadora


def need_bytes(model, ctx, k_type="f16", v_type="f16"):
    """Memoria total que necesita el modelo: pesos + caché KV + margen de buffers de cómputo."""
    return int((model["size"] + kv_bytes(model, ctx, k_type, v_type)) * (1 + COMPUTE_FRAC))


def sub_model(model, n):
    """El modelo reducido a sus últimas `n` capas (las que llama.cpp descarga a la GPU con -ngl n)."""
    total = model.get("n_layers") or 0
    s = dict(model)
    s["n_layers"] = n
    s["size"] = int(model["size"] * n / total) if total else model["size"]
    if model.get("kv_layers"):
        s["kv_layers"] = model["kv_layers"][-n:] if n else []
    return s


def offload_layers(model, ctx, gpu_usable_bytes, k_type="f16", v_type="f16"):
    """Mayor número de capas (0…n_layers) que caben en `gpu_usable_bytes` de VRAM (política gpu_with_cpu_fallback)."""
    total = model.get("n_layers") or 0
    lo, hi = 0, total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if need_bytes(sub_model(model, mid), ctx, k_type, v_type) <= gpu_usable_bytes:
            lo = mid
        else:
            hi = mid - 1
    return lo


def host_plan(model, ctx, free_bytes, k_type="f16", v_type="f16", label="cpu_only"):
    """Plan de un modelo que corre entero en CPU/RAM del equipo del orquestador (-ngl 0, sin nodos remotos)."""
    kv = kv_bytes(model, ctx, k_type, v_type)
    need = need_bytes(model, ctx, k_type, v_type)
    res = {"ok": False, "reason": "", "model": model["name"], "ctx": ctx, "model_bytes": model["size"],
           "kv_bytes": kv, "need_bytes": need, "n_layers": model.get("n_layers"), "assignments": [],
           "tensor_split": [], "est_tok_s": None, "excluded": [], "warnings": [], "ngl": 0, "policy": label,
           "host_ram": need, "ctx_train": model.get("ctx_train"), "max_ctx": None,
           "kv_types": [k_type or "f16", v_type or "f16"], "max_tier": 0}
    if need > free_bytes:
        res["reason"] = (f"{label}: necesita {need / GiB:.1f} GiB de RAM en el equipo del orquestador y hay "
                         f"{max(0, free_bytes) / GiB:.1f} GiB disponibles (tras el margen de seguridad y las reservas)")
        return res
    res["ok"] = True
    res["warnings"].append("el modelo corre en la CPU del equipo del orquestador: más lento que en GPU")
    return res


def classify(local, rtt_ms, mbps):
    """Tier de red de un nodo: 0 local, 1 LAN rápida, 2 LAN, 3 Wi-Fi/VPN/WAN. Sin medida → 2 (no se penaliza)."""
    if local:
        return 0
    if rtt_ms is not None and rtt_ms > TIER3_RTT_MS:
        return 3
    if rtt_ms is not None and mbps is not None and rtt_ms < TIER1_RTT_MS and mbps >= TIER1_MBPS:
        return 1
    return 2


def node_tier(n):
    return n["tier"] if n.get("tier") is not None else classify(n.get("local"), n.get("rtt_ms"), n.get("mbps"))


def _fill(subset, remaining, usable):
    """Llena el subconjunto por orden de tier y de velocidad; devuelve [(nodo, bytes)] o None si no cabe."""
    out = []
    for n in sorted(subset, key=lambda n: (node_tier(n), -n["bw_gbps"], -n["cap_bytes"])):
        if remaining <= 0:
            break
        take = min(usable(n), remaining)
        out.append((n, take))
        remaining -= take
    return out if remaining <= 0 else None


def _choose(free, remaining, need, model, forced, usable, max_nodes=None):
    """Mejor subconjunto de `free` (más los nodos `forced`, ya fijados) para colocar `remaining` bytes."""
    def cost(picks):
        fo = list(forced) + [n for n, _ in picks]
        t = sum((take / need) * model["size"] / (n["bw_gbps"] * 1e9 * EFFICIENCY) + (n.get("rtt_ms") or 0.5) / 1000
                for n, take in picks)
        return (max([node_tier(n) for n in fo] or [0]), sum(1 for n in fo if node_tier(n) > 0), t, len(picks))

    if len(free) > MAX_SUBSET_NODES and not max_nodes:  # demasiados nodos: voraz por tier y velocidad
        return _fill(free, remaining, usable)
    best, best_key = None, None
    for k in range(1, min(len(free), max_nodes or len(free)) + 1):
        for sub in combinations(free, k):
            if sum(usable(n) for n in sub) < remaining:
                continue
            picks = _fill(sub, remaining, usable)
            if picks is None:
                continue
            key = cost(picks)
            if best_key is None or key < best_key:
                best, best_key = picks, key
    return best


def _largest_remainder(shares, total):
    raw = [s * total for s in shares]
    out = [int(r) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)
    for i in order[: total - sum(out)]:
        out[i] += 1
    return out


def max_context(model, usable_bytes, step=256, k_type="f16", v_type="f16"):
    """Mayor contexto (tokens, múltiplo de `step`) cuya caché KV cabe junto al modelo en `usable_bytes`,
    sin pasar del contexto con el que se entrenó el modelo. None si ni siquiera cabe el modelo."""
    room = usable_bytes / (1 + COMPUTE_FRAC) - model["size"]
    fits = lambda c: kv_bytes(model, c, k_type, v_type) <= room
    if room <= 0 or not fits(step):
        return None
    top = int(model.get("ctx_train") or 1 << 20)
    lo, hi = 1, max(1, top // step)  # en unidades de `step`; la caché crece con el contexto (búsqueda binaria)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid * step):
            lo = mid
        else:
            hi = mid - 1
    ctx = lo * step
    return min(ctx, top) if model.get("ctx_train") else ctx


def plan(model, nodes, ctx=4096, min_mbps=800.0, k_type="f16", v_type="f16", max_nodes=None):
    """nodes: lista de dicts {id,name,backend,cap_bytes,bw_gbps,mbps,rtt_ms[,layers]}. `layers` fija cuántas capas
    lleva ese nodo (el resto se reparte automáticamente). Devuelve dict JSON."""
    kv = kv_bytes(model, ctx, k_type, v_type)
    need = int((model["size"] + kv) * (1 + COMPUTE_FRAC))
    res = {"ok": False, "reason": "", "model": model["name"], "ctx": ctx, "model_bytes": model["size"],
           "kv_bytes": kv, "need_bytes": need, "n_layers": model.get("n_layers"), "assignments": [],
           "tensor_split": [], "est_tok_s": None, "excluded": [], "warnings": [],
           "ctx_train": model.get("ctx_train"), "max_ctx": None, "kv_types": [k_type or "f16", v_type or "f16"]}

    elig = []
    for n in nodes:
        if n.get("mbps") is None:
            res["excluded"].append({"node": n["name"], "reason": "red no medida"})
        elif n["mbps"] < min_mbps and not n.get("allow_slow"):
            res["excluded"].append({"node": n["name"],
                                    "reason": f"red {n['mbps']:.0f} Mbps < mínimo {min_mbps:.0f} Mbps "
                                              "(en Nodos puedes usarlo igualmente, modo forzado)"})
        elif n["cap_bytes"] - OVERHEAD_NODE < 256 << 20:
            res["excluded"].append({"node": n["name"], "reason": "memoria libre insuficiente"})
        else:
            elig.append(n)
    elig.sort(key=lambda n: (node_tier(n), -n["bw_gbps"], -n["cap_bytes"]))

    remaining, picks = need, []
    total_usable = sum((n["cap_bytes"] - OVERHEAD_NODE) * HEADROOM for n in elig)
    res["max_ctx"] = max_context(model, total_usable, k_type=k_type, v_type=v_type)
    if model.get("ctx_train") and ctx > model["ctx_train"]:
        res["warnings"].append(f"contexto {ctx} mayor que el de entrenamiento del modelo ({model['ctx_train']}): "
                               "la calidad empeora más allá de ese punto")
    usable = lambda n: int((n["cap_bytes"] - OVERHEAD_NODE) * HEADROOM)
    n_layers = model.get("n_layers")
    fixed = [n for n in elig if n.get("layers")]
    if fixed and not n_layers:
        res["warnings"].append("capas fijas ignoradas: el modelo no indica su número de capas")
        fixed = []
    fixed_layers = {}
    if fixed:
        per_layer = need / n_layers
        if sum(n["layers"] for n in fixed) > n_layers:
            res["reason"] = f"las capas fijadas ({sum(n['layers'] for n in fixed)}) superan las del modelo ({n_layers})"
            return res
        for n in fixed:
            b = int(n["layers"] * per_layer)
            if b > usable(n):
                res["reason"] = (f"{n['name']}: {n['layers']} capas necesitan {b / GiB:.1f} GiB y aporta "
                                 f"{usable(n) / GiB:.1f} GiB; baja sus capas fijas o súbele la memoria")
                return res
            fixed_layers[n["id"]] = n["layers"]
            picks.append((n, b))
        remaining = int((n_layers - sum(fixed_layers.values())) * per_layer)
    if remaining > 0:
        free = [n for n in elig if n["id"] not in fixed_layers]
        chosen = _choose(free, remaining, need, model, [n for n, _ in picks], usable, max_nodes)
        if chosen:
            picks += chosen
            remaining -= sum(t for _, t in chosen)
    if remaining > 0:
        res["reason"] = (f"no cabe: necesito {need / GiB:.1f} GiB (modelo {model['size'] / GiB:.1f} + "
                         f"KV {kv / GiB:.1f} + margen) y los nodos elegibles aportan {total_usable / GiB:.1f} GiB"
                         + ("; libre por nodo: " + ", ".join(
                             f"{n['name']} {max(0, usable(n)) / GiB:.1f} GiB" for n in elig) if elig else "")
                         + ("; excluidos: " + ", ".join(f"{e['node']} ({e['reason']})" for e in res["excluded"])
                            if res["excluded"] else "")
                         + (f"; con esta memoria cabe un contexto de hasta {res['max_ctx']} tokens"
                            if res["max_ctx"] else "")
                         + ("; el modelo va fijado a un único nodo (placement=node) y ninguno tiene sitio"
                            if max_nodes == 1 else ""))
        return res

    order = {n["id"]: i for i, n in enumerate(elig)}
    picks.sort(key=lambda p: order[p[0]["id"]])  # en orden de velocidad, también los fijados
    if fixed_layers:
        free_picks = [(n, t) for n, t in picks if n["id"] not in fixed_layers]
        rest = n_layers - sum(fixed_layers.values())
        tot = sum(t for _, t in free_picks) or 1
        auto = dict(zip([n["id"] for n, _ in free_picks],
                        _largest_remainder([t / tot for _, t in free_picks], rest))) if free_picks else {}
        layers = [fixed_layers.get(n["id"], auto.get(n["id"], 0)) for n, _ in picks]
        picks = [(n, int(ly * need / n_layers)) for (n, _), ly in zip(picks, layers)]
        shares = [ly / n_layers for ly in layers]
    else:
        shares = [t / need for _, t in picks]
        layers = _largest_remainder(shares, n_layers) if n_layers else [None] * len(picks)
    t_tok = HOST_MS / 1000
    for (n, take), share, ly in zip(picks, shares, layers):
        t_tok += share * model["size"] / (n["bw_gbps"] * 1e9 * EFFICIENCY) + (n.get("rtt_ms") or 0.5) / 1000
        res["assignments"].append({"node_id": n["id"], "name": n["name"], "backend": n["backend"],
                                   "bytes": take, "share": round(share, 4), "layers": ly,
                                   "fixed": n["id"] in fixed_layers,
                                   "bw_gbps": n["bw_gbps"], "mbps": round(n["mbps"]), "rtt_ms": n.get("rtt_ms"),
                                   "tier": node_tier(n), "tier_name": TIER_NAMES[node_tier(n)]})
    res["tensor_split"] = [round(s, 4) for s in shares]
    res["est_tok_s"] = round(1 / t_tok, 2)
    res["ok"] = True

    used_ids = {n["id"] for n, _ in picks}
    forced_slow = [n["name"] for n, _ in picks if n["mbps"] < min_mbps]
    if forced_slow:
        res["warnings"].append(f"modo forzado: {', '.join(forced_slow)} tiene una red por debajo de {min_mbps:.0f} Mbps; "
                               "vale para pruebas, la velocidad será baja")
    res["unused"] = [{"node": n["name"], "tier": node_tier(n)} for n in elig if n["id"] not in used_ids]
    top = max(node_tier(n) for n, _ in picks)
    res["max_tier"] = top
    if top >= 3:
        slow = [n["name"] for n, _ in picks if node_tier(n) == 3]
        res["warnings"].append(f"usa un enlace de tier 3 ({', '.join(slow)}: RTT > {TIER3_RTT_MS:.0f} ms): cada token "
                               "cruza ese enlace y la velocidad cae; el modelo no cabe solo en nodos de tier 0-2")
    if len(picks) == 1:
        res["warnings"].append("el modelo cabe en un solo nodo: repartirlo no daría más velocidad")
    if len(picks) >= 4:
        res["warnings"].append("4+ nodos: cada nodo añade un salto de red por token")
    if res["est_tok_s"] < 1:
        res["warnings"].append("estimación < 1 token/s: usable solo para pruebas")
    return res
