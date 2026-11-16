# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Orquestador DLLM: descubre agentes, planifica el reparto, lanza llama-server sobre túneles
autenticados hacia los rpc-server de los nodos, expone una API OpenAI-compatible y se
recupera solo si un nodo cae."""
import collections
import hmac
import http.client
import json
import os
import pkgutil
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (__version__, auth, config, devices, gguf, hw, installer, legal_notice, models, pairing, presence,
               scheduler)
from . import engine
from .ledger import HOST, Ledger
from .util import FastHTTPServer, Tunnel, log, probe_link

IDLE, LOADING, READY, RECOVERING, ERROR = "IDLE", "LOADING", "READY", "RECOVERING", "ERROR"


class Busy(Exception):
    pass


class Cancelled(Exception):
    pass


class LoadError(Exception):
    pass


class AgentError(RuntimeError):
    """Respuesta de error de un agente, con su código HTTP y su mensaje."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def same_build(a, b):
    """Misma compilación de llama.cpp: se compara el número (b12387), no el texto entre paréntesis."""
    return installer.tag_from_build(a) == installer.tag_from_build(b)


def parse_prometheus(text):
    """{nombre: valor} de un texto Prometheus (sin etiquetas; si hay varias series, se suman)."""
    out = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].split("{", 1)[0]
        try:
            out[name] = out.get(name, 0.0) + float(parts[1])
        except ValueError:
            pass
    return out


# ============================================================ cliente de agentes
def _headers(secret, method, path, data):
    bh = auth.body_hash(data)
    ts, sig = auth.sign_request(secret, method, path, bh)
    return {"X-DLLM-Time": ts, "X-DLLM-Sig": sig, "X-DLLM-Body": bh, "Content-Type": "application/json"}


def call_agent(secret, node, method, path, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else b""
    c = http.client.HTTPConnection(node.host, node.http_port, timeout=timeout)
    try:
        c.request(method, path, body=data or None, headers=_headers(secret, method, path, data))
        r = c.getresponse()
        payload = r.read()
        if r.status != 200:
            try:
                msg = json.loads(payload).get("error") or ""
            except ValueError:
                msg = payload[:200].decode("utf-8", "replace")
            raise AgentError(r.status, f"{node.id} {path}: HTTP {r.status} {msg}")
        return json.loads(payload or b"{}")
    finally:
        c.close()


def is_local_host(host):
    """¿Ese host es este mismo equipo? (loopback, la IP LAN propia o el nombre del equipo)."""
    if host in ("localhost", "::1") or host.startswith("127."):
        return True
    try:
        mine = {config.lan_ip(), socket.gethostbyname(socket.gethostname())}
        return socket.gethostbyname(host) in mine
    except OSError:
        return False


def bench_node(secret, node, size=16 << 20):
    """Enlace orquestador↔nodo. Primero la sonda del túnel autenticado (RTT + bloques de 4 MiB, ver
    util.probe_link); si el agente es anterior a 0.5.0 o no responde, el bench HTTP firmado de siempre.
    Añade `tier` (scheduler.classify) y `local`."""
    local = is_local_host(node.host)
    try:
        r = probe_link(node.host, node.rpc_port, secret)
    except Exception:
        r = _bench_http(secret, node, size)
        r["via"] = "http"
    r["local"] = local
    r["tier"] = scheduler.classify(local, r["rtt_ms"], r["mbps"])
    return r


def _bench_http(secret, node, size):
    """RTT y throughput real orquestador↔nodo (HTTP, 16 MiB en cada sentido)."""
    c = http.client.HTTPConnection(node.host, node.http_port, timeout=60)
    try:
        def req(method, path, data=b""):
            h = _headers(secret, method, path, data)  # el hash se calcula fuera del cronómetro
            t = time.perf_counter()
            c.request(method, path, body=data or None, headers=h)
            r = c.getresponse()
            r.read()
            dt = time.perf_counter() - t
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} en {path}")
            return dt

        rtts = [req("GET", "/ping") for _ in range(6)]
        up = req("POST", "/bench/up", bytes(size))
        down = req("GET", f"/bench/down?n={size}")
        mb = lambda dt: size * 8 / 1e6 / dt
        return {"rtt_ms": round(min(rtts[1:]) * 1000, 2), "up_mbps": round(mb(up)), "down_mbps": round(mb(down)),
                "mbps": round(min(mb(up), mb(down))), "at": time.time()}
    finally:
        c.close()


# ============================================================ registro de nodos
class Node:
    def __init__(self, host, http_port, static=False):
        self.host, self.http_port, self.static = host, http_port, static
        self.id = f"{host}:{http_port}"
        self.info, self.net = {}, None
        self.last_seen, self.last_error = 0.0, ""
        self.hist = collections.deque(maxlen=150)  # telemetría reciente (≈5 min a 2 s por muestra)

    @property
    def name(self):
        return self.info.get("name") or self.id

    @property
    def rpc_port(self):
        return self.info.get("rpc_port")


class Registry:
    def __init__(self, secret, node_timeout=10.0, probe_interval=3.0):
        self.secret, self.node_timeout, self.interval = secret, node_timeout, probe_interval
        self.nodes, self.lock = {}, threading.RLock()
        self.pending = {}  # ip → anuncio de un nodo sin emparejar (ver pairing.py)

    def add(self, host, http_port, static=False):
        with self.lock:
            nid = f"{host}:{http_port}"
            if nid not in self.nodes:
                self.nodes[nid] = Node(host, http_port, static)
                log(f"nodo nuevo: {nid}")
            return self.nodes[nid]

    def all(self):
        with self.lock:
            return list(self.nodes.values())

    def get(self, nid):
        with self.lock:
            return self.nodes.get(nid)

    def is_up(self, n):
        return time.time() - n.last_seen < self.node_timeout

    def up_ids(self):
        return frozenset(n.id for n in self.all() if self.is_up(n))

    def beacon_loop(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError as e:
            log(f"no puedo escuchar beacons UDP en {port}: {e} (usa --node para añadir nodos a mano)")
            return
        while True:
            data, addr = s.recvfrom(4096)
            pb = pairing.parse_beacon(data)
            if pb:
                with self.lock:
                    self.pending[addr[0]] = dict(pb, host=addr[0], t=time.time())
                continue
            d = auth.verify_beacon(self.secret, data)
            if d:
                self.add(addr[0], int(d["http_port"]))
                self.pending.pop(addr[0], None)
            else:
                log(f"beacon inválido/firma incorrecta desde {addr[0]} (ignorado)")

    def probe(self, n):
        try:
            n.info = call_agent(self.secret, n, "GET", "/info", timeout=2.5)
            n.last_seen, n.last_error = time.time(), ""
            t = n.info.get("telemetry") or {}
            if t.get("t") and (not n.hist or n.hist[-1]["t"] != t["t"]):
                n.hist.append(t)
        except Exception as e:
            n.last_error = str(e)[:200]

    def probe_loop(self):
        while True:
            ths = [threading.Thread(target=self.probe, args=(n,), daemon=True) for n in self.all()]
            for t in ths:
                t.start()
            for t in ths:
                t.join(timeout=4)
            with self.lock:  # olvida nodos descubiertos por beacon que llevan 10 min mudos
                for nid in [k for k, n in self.nodes.items()
                            if not n.static and time.time() - n.last_seen > 600 and n.last_seen]:
                    del self.nodes[nid]
            time.sleep(self.interval)


# ============================================================ clúster (modelo cargado)
class RouteError(Exception):
    """Error de enrutado/carga bajo demanda. `retry_after` (s) añade la cabecera Retry-After (429/503)."""

    def __init__(self, status, msg, retry_after=None):
        super().__init__(msg)
        self.status, self.retry_after = status, retry_after


def oai_error(status, msg):
    """Cuerpo de error en el formato estándar de OpenAI."""
    typ, code = {400: ("invalid_request_error", "invalid_request"), 429: ("requests", "rate_limit_exceeded"),
                 502: ("server_error", "bad_gateway"), 504: ("server_error", "timeout")}.get(
        status, ("server_error", "service_unavailable"))
    return {"error": {"message": msg, "type": typ, "param": None, "code": code}}


class Cluster:
    """Un SLOT de modelo: un llama-server y, en cada nodo que use, su propio rpc-server y su propio túnel HMAC.
    El slot 0 (el «principal») además hace de coordinador: crea y reúne los demás (`self.slots`) y comparte con
    ellos el registro de nodos, el gestor de modelos y el libro de memoria (`ledger`)."""

    def __init__(self, cfg, reg, slot=0, parent=None, pinned=True):
        self.cfg, self.reg = cfg, reg
        self.slot, self.parent, self.pinned, self.dead = slot, parent, pinned, False
        self.last_used = time.time()
        self.ondemand_lock = parent.ondemand_lock if parent else threading.Lock()
        self.ledger = parent.ledger if parent else Ledger(
            getattr(cfg, "vram_safety_mb", 256) << 20, getattr(cfg, "ram_safety_mb", 1024) << 20)
        self.slots = parent.slots if parent else {0: self}
        self.lock, self.work = threading.RLock(), threading.Lock()
        self.state, self.error, self.model, self.ctx = IDLE, "", None, None
        self.plan, self.proc, self.tunnels, self.used = None, None, [], []
        self.llama_port = None
        self.driver = engine.driver_for({})
        self.inflight = 0
        self.desired = None  # (nombre, ctx) que el usuario quiere tener cargado
        self.gen, self.recoveries, self.last_nodeset = 0, 0, frozenset()
        self.requests, self.req_errors = 0, 0
        self.logbuf = collections.deque(maxlen=200)
        self.last_crash = None
        self.events = parent.events if parent else collections.deque(maxlen=100)
        self.llama_build = parent.llama_build if parent else (hw.build_id(cfg.llama_bin) if cfg.llama_bin else None)
        self.models = parent.models if parent else models.Manager(cfg.models_dir)
        self.presence = parent.presence if parent else presence.Presence()  # pestañas del panel (`dllm open`)
        self.claims = parent.claims if parent else {}  # host → emparejamiento en curso
        self._local_devs = None   # dispositivos locales de llama-server (no cambian: se cachean)
        self.llama_hist = collections.deque(maxlen=150)  # tokens/s y peticiones (de /metrics)
        self._sync_tried = {}     # nodo → hora del último intento de sincronizar llama.cpp
        self.mmproj = None

    # ---------------------------------------------------------- utilidades
    def event(self, msg):
        msg = f"[slot {self.slot}] {msg}" if self.slot else msg
        log(msg)
        self.events.append({"t": time.time(), "msg": msg})

    def _set(self, state, error=""):
        with self.lock:
            self.state, self.error = state, error

    def _chk(self, gen):
        if gen != self.gen:
            raise Cancelled()

    def resolve_model(self, name):
        root = Path(self.cfg.models_dir).resolve()
        p = (root / name).resolve()
        if root not in p.parents or p.suffix != ".gguf" or not p.is_file():
            raise ValueError(f"modelo no válido o fuera de {root}: {name}")
        return str(p)

    def list_models(self):
        return self.models.list()

    def delete_model(self, name):
        if name == self.model and self.state != IDLE:
            raise Busy("ese modelo está cargado: descárgalo antes de eliminarlo")
        self.models.delete(name)
        self.event(f"modelo eliminado: {name}")

    # ---------------------------------------------------------- planificación
    def bench_all(self, nodes, force):
        # SECUENCIAL a propósito: en paralelo los nodos se repartirían el enlace del orquestador
        # y todos parecerían más lentos de lo que son.
        for n in nodes:
            if not force and n.net and time.time() - n.net["at"] < 60:
                continue
            try:
                n.net = bench_node(self.reg.secret, n)
            except Exception as e:
                n.net, n.last_error = None, f"bench: {e}"[:200]

    # ---------------------------------------------------------- ajustes por nodo (en el orquestador)
    def models_name(self, mpath):
        try:
            return str(Path(mpath).resolve().relative_to(self.models.root)).replace(os.sep, "/")
        except ValueError:
            return os.path.basename(mpath)

    def _node_file(self):
        return config.home() / "nodes.json"

    def all_node_settings(self):
        try:
            return json.loads(self._node_file().read_text())
        except (OSError, ValueError):
            return {}

    def node_settings(self, name):
        return self.all_node_settings().get(name, {})

    def set_node_settings(self, req):
        n = self.reg.get(req.get("node") or "")
        if not n:
            raise ValueError("nodo desconocido")
        cur = self.node_settings(n.name)
        if "enabled" in req:
            if not isinstance(req["enabled"], bool):
                raise ValueError("'enabled' debe ser true o false")
            cur["enabled"] = req["enabled"]
        for k, lo, hi, typ in (("layers", 1, 4096, int), ("bw_gbps", 1.0, 10000.0, float)):
            if k in req:
                v = req[k]
                if v in (None, ""):
                    cur.pop(k, None)
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"'{k}' debe ser un número")
                if typ is int and f != int(f) or not lo <= f <= hi:
                    raise ValueError(f"'{k}' debe ser {'un entero ' if typ is int else ''}entre {lo:g} y {hi:g}")
                cur[k] = typ(f)
        allns = self.all_node_settings()
        allns[n.name] = cur
        self._node_file().parent.mkdir(parents=True, exist_ok=True)
        self._node_file().write_text(json.dumps(allns, indent=2, ensure_ascii=False))
        self.event(f"ajustes de {n.name}: {cur or 'automático'}"
                   + (" (se aplican al recargar el modelo)" if n.id in self.used else ""))
        return {"ok": True, "settings": cur, "in_use": n.id in self.used}

    # ---------------------------------------------------------- métricas de llama-server
    def metrics_loop(self):
        """Lee /metrics de llama-server (Prometheus) y calcula tokens/s en directo."""
        prev = None
        while not self.dead:
            time.sleep(2)
            if self.state != READY or not self.llama_port:
                prev = None
                continue
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.llama_port, timeout=2)
                c.request("GET", "/metrics")
                r = c.getresponse()
                text = r.read().decode("utf-8", "replace") if r.status == 200 else ""
                c.close()
            except OSError:
                continue
            m = parse_prometheus(text)
            now = time.time()
            gen, prompt = m.get("llamacpp:tokens_predicted_total"), m.get("llamacpp:prompt_tokens_total")
            s = {"t": now, "requests": m.get("llamacpp:requests_processing"),
                 "deferred": m.get("llamacpp:requests_deferred"), "kv_usage": m.get("llamacpp:kv_cache_usage_ratio"),
                 "tok_s": None, "prompt_tok_s": None, "tokens_total": gen}
            if prev and gen is not None and prev.get("tokens_total") is not None:
                dt = now - prev["t"]
                s["tok_s"] = round(max(0.0, gen - prev["tokens_total"]) / dt, 2)
                if prompt is not None and prev.get("prompt_total") is not None:
                    s["prompt_tok_s"] = round(max(0.0, prompt - prev["prompt_total"]) / dt, 1)
            s["prompt_total"] = prompt
            prev = s
            self.llama_hist.append(s)

    def telemetry(self):
        def compact(t):
            g = t.get("gpu") or {}
            return [round(t["t"], 1), g.get("util"), g.get("vram_used"), g.get("power"), g.get("temp"),
                    t.get("cpu"), (t.get("rpc_rx_bps") or 0) + (t.get("rpc_tx_bps") or 0)]
        nodes = []
        for n in self.reg.all():
            i = n.info
            nodes.append({"id": n.id, "name": n.name, "up": self.reg.is_up(n), "used": any(n.id in c.used for c in list(self.slots.values())),
                          "backend": i.get("backend"), "device_desc": i.get("device_desc"), "cores": i.get("cores"),
                          "threads": i.get("threads"), "cap_bytes": i.get("cap_bytes"),
                          "settings": self.node_settings(n.name), "now": i.get("telemetry") or {},
                          "hist": [compact(t) for t in n.hist],
                          "hist_cols": ["t", "gpu_util", "vram_used", "power", "temp", "cpu", "rpc_bps"]})
        lh = list(self.llama_hist)
        return {"t": time.time(), "state": self.state, "model": self.model,
                "llama": lh[-1] if lh else {}, "llama_hist": [[round(x["t"], 1), x.get("tok_s"), x.get("requests")]
                                                             for x in lh], "nodes": nodes}

    def host_free_ram(self, skip):
        """RAM utilizable en el equipo del orquestador: la libre de verdad − margen − reservas de otros slots."""
        total, avail = hw.mem_info()
        return self.ledger.available_ram(HOST, total, avail, skip)

    def make_plan(self, mpath, ctx, force_bench=True, ignore=()):
        """Plan de reparto. `ignore`: slots cuya memoria se da por liberada (¿cabría si los expulsara?)."""
        m = gguf.read_model(mpath)
        ms = self.models.settings(self.models_name(mpath))
        skip = {self.slot} | set(ignore)
        kt, vt = ms.get("cache_type_k"), ms.get("cache_type_v")
        policy = ms.get("device_policy", "auto")
        if policy == "cpu_only":  # sin nodos remotos: todo en RAM del orquestador
            p = scheduler.host_plan(m, ctx, self.host_free_ram(skip), kt, vt, "cpu_only")
            p["reserve"] = {"vram": {}, "ram": {HOST: p["host_ram"]}} if p["ok"] else {}
            p["placement"] = "host"
            return m, p
        nodes, excluded, ok_nodes, excluded_note = self.reg.all(), [], [], []
        up = [n for n in nodes if self.reg.is_up(n)]
        excluded += [{"node": n.name, "reason": "sin respuesta"} for n in nodes if n not in up]
        off = [n for n in up if self.node_settings(n.name).get("enabled") is False]
        excluded += [{"node": n.name, "reason": "desactivado en el panel"} for n in off]
        up = [n for n in up if n not in off]
        self.bench_all(up, force_bench)
        for n in up:
            i = n.info
            gpu = i.get("backend", "cpu") != "cpu"
            if not i.get("rpc_bin"):
                excluded.append({"node": n.name, "reason": "rpc-server no instalado"})
            elif self.llama_build and i.get("rpc_build") and not same_build(i["rpc_build"], self.llama_build):
                up_ = i.get("update") or {}
                if up_.get("state") == "descargando":
                    why = f"actualizando llama.cpp a {up_.get('tag')} ({up_.get('progress', 0)} %)"
                elif up_.get("state") == "error":
                    why = f"build distinta y la actualización falló: {up_.get('error')}"
                else:
                    why = (f"build distinta ({i['rpc_build']} ≠ {self.llama_build})"
                           + ("; se le pedirá actualizar" if i.get("auto_update") and self.cfg_auto_sync()
                              else ": reinstala el nodo con el código de unión actual"))
                excluded.append({"node": n.name, "reason": why})
            elif self.slot and "rpc_slots" not in i:
                excluded.append({"node": n.name, "reason": "su agente es anterior a 0.5.0 y no admite varios "
                                                           "modelos a la vez: actualízalo"})
            elif policy in ("gpu_strict", "gpu_with_cpu_fallback") and not gpu:
                excluded.append({"node": n.name, "reason": f"{policy}: el nodo no tiene GPU"})
            elif gpu and self.ledger.available_ram(n.id, i.get("ram_total") or 0, i.get("ram_free"), skip) \
                    < scheduler.IO_RAM and i.get("ram_total"):
                excluded.append({"node": n.name, "reason": "queda muy poca RAM libre en el nodo (margen de seguridad)"})
            else:
                ns = self.node_settings(n.name)
                cap0 = i.get("cap_bytes", 0)
                cap = self.ledger.available_pool(n.id, cap0, "vram" if gpu else "ram", skip)
                if cap < cap0:
                    excluded_note.append(f"{n.name}: {(cap0 - cap) / hw.GiB:.1f} GiB reservados por otros modelos")
                ok_nodes.append({"id": n.id, "name": n.name, "backend": i.get("backend", "cpu"), "gpu": gpu,
                                 "cap_bytes": cap,
                                 "bw_gbps": ns.get("bw_gbps") or i.get("bw_gbps", 10),
                                 "layers": ns.get("layers"),
                                 "mbps": n.net["mbps"] if n.net else None,
                                 "rtt_ms": n.net["rtt_ms"] if n.net else None,
                                 "local": n.net.get("local") if n.net else None,
                                 "tier": n.net.get("tier") if n.net else None})
        place = ms.get("placement", "auto")
        if place == "auto":
            place = engine.driver_for(ms).default_placement()
        mx = 1 if place == "node" else None
        p = scheduler.plan(m, ok_nodes, ctx, self.cfg.min_mbps, kt, vt, max_nodes=mx)
        p["policy"] = policy
        if not p["ok"] and policy == "gpu_with_cpu_fallback":
            p = self._fallback_plan(m, ok_nodes, ctx, kt, vt, mx, skip, p)
        p["placement"] = place
        p["excluded"] = excluded + p["excluded"]
        if excluded_note:
            p["warnings"] = p.get("warnings", []) + ["memoria ya reservada: " + "; ".join(excluded_note)]
        if p["ok"]:
            by = {n["id"]: n for n in ok_nodes}
            vr, rr = {}, {}
            for a in p["assignments"]:
                if by[a["node_id"]].get("gpu"):
                    vr[a["node_id"]] = a["bytes"] + scheduler.OVERHEAD_NODE
                    rr[a["node_id"]] = scheduler.IO_RAM
                else:
                    rr[a["node_id"]] = a["bytes"] + scheduler.OVERHEAD_NODE
            if p.get("host_ram"):
                rr[HOST] = p["host_ram"]
            p["reserve"] = {"vram": vr, "ram": rr}
        return m, p

    def _fallback_plan(self, m, ok_nodes, ctx, kt, vt, mx, skip, full):
        """gpu_with_cpu_fallback: lo que quepa en VRAM (-ngl N) y el resto en la RAM del orquestador; si ni una capa
        cabe en GPU, todo en CPU (-ngl 0)."""
        gpu_nodes = [n for n in ok_nodes if n.get("gpu")]
        usable = sum(max(0, (n["cap_bytes"] - scheduler.OVERHEAD_NODE) * scheduler.HEADROOM) for n in gpu_nodes)
        n_gpu = scheduler.offload_layers(m, ctx, usable, kt, vt) if gpu_nodes and m.get("n_layers") else 0
        free = self.host_free_ram(skip)
        if n_gpu >= 1:
            sub = scheduler.sub_model(m, n_gpu)
            p2 = scheduler.plan(sub, gpu_nodes, ctx, self.cfg.min_mbps, kt, vt, max_nodes=mx)
            if p2["ok"]:
                host = scheduler.need_bytes(m, ctx, kt, vt) - p2["need_bytes"]
                if host <= free:
                    p2.update(ngl=n_gpu, host_ram=host, policy="gpu_with_cpu_fallback", model=m["name"],
                              n_layers_total=m.get("n_layers"), model_bytes=m["size"])
                    p2["warnings"].append(f"solo {n_gpu} de {m['n_layers']} capas caben en GPU: el resto "
                                          f"({host / hw.GiB:.1f} GiB) corre en la CPU del equipo del orquestador")
                    return p2
        hp = scheduler.host_plan(m, ctx, free, kt, vt, "gpu_with_cpu_fallback")
        if hp["ok"]:
            hp["warnings"].insert(0, f"no cabe en GPU ({full['reason']}): se ejecuta entero en CPU (-ngl 0)")
            hp["excluded"] = full.get("excluded", [])
            return hp
        full["reason"] += f" · alternativa en CPU: {hp['reason']}"
        return full

    # ---------------------------------------------------------- carga / descarga
    def request_load(self, name, ctx, pinned=None):
        path = self.resolve_model(name)
        for k, c in list(self.slots.items()):
            if k != self.slot and c.model == name and c.state in (LOADING, READY, RECOVERING):
                raise Busy(f"{name} ya está cargado en el slot {k}")
        if pinned is not None:
            self.pinned = bool(pinned)
        with self.lock:
            if self.state == LOADING:
                raise Busy("ya hay una carga en curso")
            self.gen += 1
            gen = self.gen
            self.desired = (name, ctx)
        threading.Thread(target=self._load_worker, args=(path, name, ctx, gen, False), daemon=True).start()

    # ---------------------------------------------------------- slots (varios modelos a la vez)
    def new_slot(self, pinned=True):
        """Crea un slot vacío (solo en el coordinador). Cada slot tiene su llama-server, sus rpc-server y su túnel."""
        with self.lock:
            free = [i for i in range(1, auth.MAX_SLOTS) if i not in self.slots]
            if not free:
                raise Busy(f"máximo de {auth.MAX_SLOTS} modelos simultáneos")
            c = Cluster(self.cfg, self.reg, slot=free[0], parent=self, pinned=pinned)
            self.slots[free[0]] = c
        for fn in (c.watchdog, c.metrics_loop):
            threading.Thread(target=fn, daemon=True).start()
        return c

    def load_model(self, name, ctx, slot=0, pinned=True):
        """Carga `name` en un slot: 0 (principal), un número existente o «new» (slot nuevo)."""
        if slot in (None, 0, "0"):
            self.slots[0].request_load(name, ctx, pinned)
            return 0
        if slot == "new":
            c = self.new_slot(pinned)
            try:
                c.request_load(name, ctx, pinned)
            except Exception:
                self.drop_slot(c.slot)
                raise
            return c.slot
        c = self.slots.get(int(slot))
        if c is None:
            raise ValueError(f"el slot {slot} no existe")
        c.request_load(name, ctx, pinned)
        return c.slot

    def drop_slot(self, slot):
        """Descarga el slot; los secundarios además desaparecen (el 0 queda vacío)."""
        c = self.slots.get(int(slot))
        if c is None:
            raise ValueError(f"el slot {slot} no existe")
        c.unload()
        if c.slot:
            c.dead = True
            self.slots.pop(c.slot, None)

    # ---------------------------------------------------------- enrutado L7 por modelo y capacidad
    @staticmethod
    def _names(c):
        n = c.model or ""
        base = os.path.basename(n)
        return {n.lower(), base.lower(), (base[:-5] if base.endswith(".gguf") else base).lower()}

    def find_slot(self, model, states=(READY,)):
        m = (model or "").lower()
        for k in sorted(self.slots):
            c = self.slots[k]
            if c.state in states and c.model and m in self._names(c):
                return c
        return None

    def default_slot(self, cap):
        """Slot listo con esa capacidad: el principal si vale; si no, el usado más recientemente."""
        ok = [c for c in self.slots.values() if c.state == READY and (cap is None or cap in c.driver.capabilities())]
        if not ok:
            return None
        return next((c for c in ok if c.slot == 0), None) or max(ok, key=lambda c: c.last_used)

    def disk_model(self, model):
        """Nombre de un .gguf de la carpeta de modelos que corresponde a `model` (o None)."""
        m = (model or "").lower()
        if not m:
            return None
        for it in self.list_models():
            nm = it["name"]
            base = os.path.basename(nm)
            if m in (nm.lower(), base.lower(), (base[:-5] if base.endswith(".gguf") else base).lower()):
                return nm
        return None

    def route(self, model, cap):
        """Elige el slot que atiende la petición. Lanza RouteError(estado, mensaje)."""
        deadline = time.time() + getattr(self.cfg, "queue_timeout", 30.0)
        c = self.find_slot(model)
        if c is None and model and getattr(self.cfg, "on_demand", False):
            nm = self.disk_model(model)
            if nm and not self.find_slot(nm, (LOADING, RECOVERING)):
                c = self.load_on_demand(nm, deadline)
            elif nm:
                c = self.wait_slot(nm)
        if c is None:
            c = self.default_slot(cap)  # nombre desconocido o ausente: como siempre, el modelo cargado
        if c is None:
            st = self.slots[0].state
            raise RouteError(503, f"modelo no listo (estado {st})")
        if cap and cap not in c.driver.capabilities():
            raise RouteError(400, f"el modelo {c.model} no admite {cap} (admite: {', '.join(sorted(c.driver.capabilities()))})")
        return c

    def wait_slot(self, name, timeout=None):
        end = time.time() + (timeout or self.cfg.load_timeout + 60)
        while time.time() < end:
            c = self.find_slot(name, (READY,))
            if c:
                return c
            if not self.find_slot(name, (LOADING, RECOVERING)):
                raise RouteError(503, f"no se pudo cargar {name}")
            time.sleep(0.3)
        raise RouteError(504, f"{name} no estuvo listo a tiempo")

    def lru_victim(self, exclude=None):
        """Slot on-demand listo y sin peticiones en curso que lleva más tiempo sin usarse."""
        cand = [c for c in self.slots.values() if c is not exclude and not c.pinned and c.state == READY
                and c.inflight == 0]
        return min(cand, key=lambda c: c.last_used) if cand else None

    def load_on_demand(self, name, deadline=None):
        """Carga `name` en un slot nuevo y espera a que esté listo.

        - Si falta memoria, expulsa por LRU los slots no fijados, listos y SIN peticiones en curso (nunca mata una
          generación a medias).
        - Si el modelo no cabría ni expulsando todo lo que no está fijado, se rechaza ANTES de lanzar nada (503).
        - Si cabría pero los demás están ocupados, la petición espera en una cola acotada por `queue_timeout`;
          vencido el plazo, 429 con Retry-After. La cola es el propio cerrojo: una carga a la vez."""
        deadline = deadline or time.time() + getattr(self.cfg, "queue_timeout", 30.0)
        if not self.ondemand_lock.acquire(timeout=max(0.0, deadline - time.time())):
            raise RouteError(429, "hay otra carga de modelo en curso y se agotó el tiempo de espera de la cola",
                             retry_after=5)
        slot = None
        try:
            c = self.find_slot(name)
            if c:
                return c
            ctx = int(self.models.settings(name).get("ctx") or 4096)
            path = self.resolve_model(name)
            slot = self.new_slot(pinned=False)
            while True:
                _, p = slot.make_plan(path, ctx, force_bench=False)
                if p["ok"]:
                    break
                movable = [c for c in self.slots.values() if c is not slot and not c.pinned
                           and c.state in (READY, LOADING, RECOVERING)]
                # rechazo previo: si no cabe ni sin ningún modelo no fijado, no se expulsa nada en vano
                _, p2 = slot.make_plan(path, ctx, force_bench=False, ignore={c.slot for c in movable})
                if not p2["ok"]:
                    raise RouteError(503, f"{name} no cabe ni descontando los modelos fijados: {p2['reason']}")
                victim = self.lru_victim(exclude=slot)
                if victim is not None:
                    self.event(f"memoria insuficiente para {name}: se expulsa {victim.model} (LRU)")
                    self.drop_slot(victim.slot)
                    continue
                if time.time() >= deadline:
                    raise RouteError(429, f"todos los recursos están ocupados por modelos en uso; {name} espera "
                                          "a que alguno quede libre", retry_after=max(5, int(self.cfg.queue_timeout)))
                time.sleep(0.3)  # cola: reintenta cuando algún slot pase a IDLE
            self.event(f"carga bajo demanda: {name}")
            slot.request_load(name, ctx, False)
            end = time.time() + self.cfg.load_timeout + 60
            while slot.state in (IDLE, LOADING, RECOVERING) and time.time() < end:
                time.sleep(0.3)
            if slot.state != READY:
                raise RouteError(503, f"no se pudo cargar {name}: {slot.error or slot.state}")
            return slot
        except Exception:
            if slot is not None and slot.slot in self.slots and slot.state != READY:
                self.drop_slot(slot.slot)
            raise
        finally:
            self.ondemand_lock.release()

    def models_listing(self):
        data = []
        loaded = set()
        for k in sorted(self.slots):
            c = self.slots[k]
            if c.model and c.state == READY:
                loaded.add(c.model)
                data.append({"id": c.model, "object": "model", "owned_by": "dllm",
                             "dllm": {"slot": k, "state": c.state, "pinned": c.pinned,
                                      "activity": "BUSY" if c.inflight else "IDLE",
                                      "capabilities": sorted(c.driver.capabilities())}})
        if getattr(self.cfg, "on_demand", False):
            for it in self.list_models():
                if it["name"] not in loaded and not it["name"].startswith("mmproj"):
                    data.append({"id": it["name"], "object": "model", "owned_by": "dllm",
                                 "dllm": {"state": "bajo demanda"}})
        return {"object": "list", "data": data}

    def slots_view(self):
        out = []
        for k in sorted(self.slots):
            c = self.slots[k]
            out.append({"slot": k, "model": c.model, "state": c.state, "error": c.error, "ctx": c.ctx,
                        "pinned": c.pinned, "last_used": c.last_used, "llama_port": c.llama_port,
                        "nodes": list(c.used), "est_tok_s": (c.plan or {}).get("est_tok_s"),
                        "active_requests": c.inflight, "activity": "BUSY" if c.inflight else "IDLE",
                        "policy": (c.plan or {}).get("policy"),
                        "reserved": dict(self.ledger.res.get(k, {}))})
        return out

    def unload(self):
        with self.lock:
            self.gen += 1
            self.desired = None
        with self.work:
            self._teardown()
            self.model, self.plan = None, None
            self._set(IDLE)
        self.event("modelo descargado")

    def _load_worker(self, path, name, ctx, gen, recovering):
        with self.work:
            if gen != self.gen:
                return
            self._teardown()
            self.model, self.ctx = name, ctx
            self._set(RECOVERING if recovering else LOADING)
            self.last_nodeset = self._nodesig()
            try:
                if not self.cfg.llama_bin:
                    raise LoadError("llama-server no encontrado (--bin-dir / --llama-bin)")
                self.event(f"{'recuperando' if recovering else 'cargando'} {name} (ctx {ctx})")
                if recovering:
                    time.sleep(1.5)  # deja que los agentes liberen memoria antes de medir capacidad
                with self.ledger.plan_lock:  # planificar y reservar de una vez: dos cargas no ven la misma memoria
                    _, plan = self.make_plan(path, ctx)
                    self._chk(gen)
                    self.plan = plan
                    if not plan["ok"]:
                        raise LoadError(plan["reason"])
                    rsv = plan.get("reserve") or {}
                    self.ledger.reserve(self.slot, rsv.get("vram"), rsv.get("ram"))
                used = [self.reg.get(a["node_id"]) for a in plan["assignments"]]
                for n in used:
                    self.event(f"arrancando rpc-server en {n.name}" + (f" (slot {self.slot})" if self.slot else ""))
                    call_agent(self.reg.secret, n, "POST", "/rpc/start", {"slot": self.slot}, timeout=40)
                    self._chk(gen)
                self.used = [n.id for n in used]
                self.tunnels = [Tunnel(n.host, n.rpc_port, self.reg.secret, self.slot).start() for n in used]
                self._launch_llama(path, ctx, plan)
                self._wait_ready(gen)
                self._set(READY)
                self.event(f"{name} listo en {len(used)} nodo(s)"
                           + (f" · ~{plan['est_tok_s']} tok/s estimados" if plan.get("est_tok_s") else
                              " · en CPU del orquestador"))
            except Cancelled:
                self._teardown()
            except Exception as e:
                self.event(f"error: {e}")
                self._teardown()
                self._set(ERROR, str(e))

    def _launch_llama(self, path, ctx, plan):
        self.llama_port = hw.slot_port({c.llama_port for c in self.slots.values() if c is not self})
        st = self.models.settings(self.model)
        self.driver = engine.driver_for(st)
        self.mmproj = None
        mp = None
        if "embeddings" not in self.driver.capabilities():  # un modelo de embeddings no lleva proyector
            try:
                mp = self.models.mmproj_for(self.model)
            except (models.ModelError, OSError, ValueError) as e:  # un proyector roto no impide cargar el texto
                self.event(f"proyector multimodal no disponible ({e}): se carga solo texto")
        if mp:
            self.mmproj = os.path.basename(mp)
            self.event(f"multimodal: proyector {self.mmproj}")
        cmd = self.driver.build_command({
            "bin": self.cfg.llama_bin, "model_path": path, "port": self.llama_port, "ctx": ctx,
            "rpc": [f"127.0.0.1:{t.port}" for t in self.tunnels], "tensor_split": plan["tensor_split"],
            "ngl": plan.get("ngl", 999), "device_args": self._device_args(plan) if self.tunnels else [],
            "mmproj": mp, "extra": self.cfg.llama_arg or []})
        self.event("lanzando: " + " ".join(cmd))
        self.logbuf.clear()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=lambda p: [self.logbuf.append(l.rstrip()) for l in p.stdout],
                         args=(self.proc,), daemon=True).start()

    def _device_args(self, plan):
        """Fija los dispositivos de llama-server a los remotos (uno por nodo, en el orden de --rpc).

        Se listan los dispositivos sin y con --rpc: la diferencia son los remotos. Así la GPU local del orquestador no
        entra en el reparto (rol Ambos en un PC con GPU) y no dependemos de cómo nombre llama.cpp a los RPC."""
        rpc = [f"127.0.0.1:{t.port}" for t in self.tunnels]
        try:
            if self._local_devs is None:
                self._local_devs = devices.probe_llama_server(self.cfg.llama_bin)
            rem = devices.remote(self._local_devs, devices.probe_llama_server(self.cfg.llama_bin, rpc))
        except RuntimeError as e:
            self.event(f"aviso: no se pudieron listar los dispositivos ({e}); se lanza sin --device")
            return []
        if not rem:
            self.event("aviso: esta versión de llama-server no lista los dispositivos remotos; se lanza sin --device")
            return []
        if len(rem) != len(rpc):
            raise LoadError(f"los nodos exponen {len(rem)} dispositivos y se esperaban {len(rpc)} (uno por nodo): "
                            "actualiza los agentes o fija el dispositivo de cada nodo con --device")
        for d, a in zip(rem, plan["assignments"]):
            if a["bytes"] > d["free"]:
                self.event(f"aviso: a {a['name']} se le asignan {a['bytes'] / hw.GiB:.1f} GiB y su dispositivo "
                           f"tiene {d['free'] / hw.GiB:.1f} GiB libres")
        self.event("dispositivos: " + ", ".join(f"{a['name']} → {d['desc']} ({d['free'] / hw.GiB:.1f} GiB libres)"
                                               for d, a in zip(rem, plan["assignments"])))
        return ["--device", ",".join(d["name"] for d in rem)]

    def _wait_ready(self, gen):
        deadline = time.time() + self.cfg.load_timeout
        while True:
            self._chk(gen)
            if self.proc.poll() is not None:
                raise LoadError("llama-server terminó: " + " | ".join(list(self.logbuf)[-6:]))
            if self.driver.healthcheck(self.llama_port):
                return
            if time.time() > deadline:
                raise LoadError(f"llama-server no estuvo listo en {self.cfg.load_timeout:.0f} s")
            time.sleep(0.5)

    def _teardown(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(8)
            except subprocess.TimeoutExpired:
                p.kill()
        for t in self.tunnels:
            t.stop()
        self.tunnels = []
        self.ledger.release(self.slot)
        for nid in self.used:
            n = self.reg.get(nid)
            if n and self.reg.is_up(n):
                try:
                    call_agent(self.reg.secret, n, "POST", "/rpc/stop", {"slot": self.slot}, timeout=3)
                except Exception:
                    pass
        self.used = []

    # ---------------------------------------------------------- vigilancia
    def cfg_auto_sync(self):
        return not getattr(self.cfg, "no_auto_sync", False)

    def sync_builds(self):
        """Pide a los nodos con otra versión de llama.cpp que instalen la del orquestador."""
        if not (self.cfg_auto_sync() and self.llama_build):
            return
        tag = installer.tag_from_build(self.llama_build)
        if not tag:
            return
        for n in self.reg.all():
            i = n.info
            if (not self.reg.is_up(n) or not i.get("auto_update") or not i.get("rpc_build")
                    or same_build(i["rpc_build"], self.llama_build) or (i.get("update") or {}).get("state") == "descargando"):
                continue
            if time.time() - self._sync_tried.get(n.id, 0) < 900:  # como mucho un intento cada 15 min
                continue
            self._sync_tried[n.id] = time.time()
            try:
                call_agent(self.reg.secret, n, "POST", "/llama/install", {"tag": tag}, timeout=10)
                self.event(f"{n.name}: tiene llama.cpp {i['rpc_build']}; se le pide instalar {tag}")
            except Exception as e:
                self.event(f"{n.name}: no se pudo pedir la actualización de llama.cpp ({e})")

    def _nodesig(self):
        return frozenset((n.id, n.info.get("rpc_build")) for n in self.reg.all() if self.reg.is_up(n))

    def watchdog(self):
        tick = 0
        while not self.dead:
            time.sleep(1)
            tick += 1
            if tick % 5 == 0 and not self.slot:
                try:
                    self.sync_builds()
                except Exception as e:
                    log("sync_builds:", e)
            with self.lock:
                st, desired = self.state, self.desired
            if st == READY and desired:
                why = None
                if self.proc is None or self.proc.poll() is not None:
                    why = self._crash_report()
                else:
                    for nid in self.used:
                        n = self.reg.get(nid)
                        if not n or not self.reg.is_up(n):
                            why = f"nodo {n.name if n else nid} caído"
                            break
                if why:
                    # el estado cambia YA (no cuando arranque el worker): nadie debe ver READY con un nodo caído
                    self._set(RECOVERING)
                    self.recoveries += 1
                    self.event(f"{why}: replanificando con los nodos disponibles")
                    self._start_worker(desired, True)
            elif st == ERROR and desired and self._nodesig() != self.last_nodeset:
                self.event("cambió el conjunto de nodos: reintentando carga")
                self._start_worker(desired, False)

    def _crash_report(self):
        """Motivo de la caída de llama-server: código de salida y líneas relevantes de su log, que se borra al relanzar."""
        rc = self.proc.poll() if self.proc else None
        tail = [l for l in list(self.logbuf)[-60:] if l.strip()]
        key = [l for l in tail if re.search(r"assert|error|abort|fail|out of memory|oom|exception|signal", l, re.I)]
        lines = [l[:300] for l in (key or tail)[-6:]]
        for l in lines:
            self.event(f"  llama-server: {l}")
        self.last_crash = {"t": time.time(), "code": rc, "lines": lines}
        return "llama-server terminó" + (f" (código {rc})" if rc is not None else "")

    def _start_worker(self, desired, recovering):
        try:
            path = self.resolve_model(desired[0])
        except ValueError:
            return
        with self.lock:
            self.gen += 1
            gen = self.gen
        threading.Thread(target=self._load_worker, args=(path, desired[0], desired[1], gen, recovering),
                         daemon=True).start()

    # ---------------------------------------------------------- límites de los nodos
    def set_node_limits(self, req):
        n = self.reg.get(req.get("node") or "")
        if not n:
            raise ValueError("nodo desconocido")
        body = {k: req.get(k) for k in ("share", "mem_gb", "threads", "reset", "device") if k in req}
        try:
            r = call_agent(self.reg.secret, n, "POST", "/limits", body, timeout=5)
        except AgentError as e:
            if e.status in (400, 403):  # validación o nodo en modo manual: mensaje para el usuario
                raise ValueError(str(e).split(f"HTTP {e.status} ", 1)[-1])
            raise
        self.reg.probe(n)  # refresca el panel al momento
        in_use = n.id in self.used
        self.event(f"límites de {n.name}: {r['cap_bytes'] / hw.GiB:.1f} GiB · {r['threads']} hilos"
                   + (" (se aplican al recargar el modelo)" if in_use else ""))
        return {"ok": True, "cap_bytes": r["cap_bytes"], "threads": r["threads"], "in_use": in_use}

    # ---------------------------------------------------------- estado / métricas
    # ------------------------------------------------------------ emparejamiento de nodos sin código
    def pending_view(self):
        now = time.time()
        with self.reg.lock:
            for ip in [ip for ip, p in self.reg.pending.items() if now - p["t"] > 30]:
                self.reg.pending.pop(ip, None)
            pend = [{"host": p["host"], "name": p["name"], "http_port": p["http_port"]}
                    for p in self.reg.pending.values()]
        claims = [{k: c[k] for k in ("host", "name", "sas", "state", "detail")} for c in self.claims.values()
                  if now - c["t"] < 600]
        return {"pending": pend, "claims": claims}

    def claim(self, host):
        """Reclama un nodo sin emparejar: intercambio de claves, código de verificación y espera a que se acepte
        en el nodo; después le envía el secreto cifrado. Devuelve el código de verificación a mostrar."""
        with self.reg.lock:
            p = self.reg.pending.get(host)
        if not p:
            raise ValueError("ese equipo ya no está esperando unirse (¿se ha apagado o ya está emparejado?)")
        x, pub = pairing.keypair()
        c = http.client.HTTPConnection(host, p["http_port"], timeout=8)
        try:
            body = json.dumps({"orch": socket.gethostname(), "pub": str(pub)})
            c.request("POST", "/pair/request", body=body, headers={"Content-Type": "application/json"})
            r = c.getresponse()
            data = json.loads(r.read() or b"{}")
            if r.status != 200:
                raise ValueError(data.get("error") or f"HTTP {r.status}")
        finally:
            c.close()
        key = pairing.shared_key(x, data["pub"])
        code = pairing.sas(key, str(pub), data["pub"])
        cl = {"host": host, "port": p["http_port"], "name": p["name"], "id": data["id"], "key": key, "sas": code,
              "state": "esperando confirmación en el nodo", "detail": "", "t": time.time()}
        self.claims[host] = cl
        self.event(f"reclamando {p['name']} ({host}): código de verificación {code}")
        threading.Thread(target=self._claim_worker, args=(cl,), daemon=True).start()
        return {"sas": code, "name": p["name"]}

    def _orch_ip_for(self, host):
        if self.cfg_advertise():
            return self.cfg_advertise()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 9))  # no envía nada: solo elige la interfaz por la que se llega al nodo
            return s.getsockname()[0]
        finally:
            s.close()

    def cfg_advertise(self):
        return getattr(self.cfg, "advertise_host", None)

    def _claim_worker(self, cl):
        def get(path, body=None):
            c = http.client.HTTPConnection(cl["host"], cl["port"], timeout=8)
            try:
                c.request("POST" if body is not None else "GET", path,
                          body=json.dumps(body) if body is not None else None,
                          headers={"Content-Type": "application/json"})
                r = c.getresponse()
                return r.status, json.loads(r.read() or b"{}")
            finally:
                c.close()

        deadline = cl["t"] + pairing.REQUEST_TTL
        try:
            while time.time() < deadline:
                st, d = get(f"/pair/status?id={cl['id']}")
                state = d.get("state")
                if state == "aceptada":
                    box = pairing.seal(cl["key"], {
                        "secret": self.reg.secret, "orch_ip": self._orch_ip_for(cl["host"]),
                        "beacon_port": self.cfg.beacon_port, "orch_name": socket.gethostname(),
                        "tag": installer.tag_from_build(self.llama_build)})
                    st, d = get("/pair/complete", {"id": cl["id"], "box": box})
                    if st != 200:
                        raise RuntimeError(d.get("error") or f"HTTP {st}")
                    cl["state"] = "unido"
                    self.event(f"{cl['name']} ({cl['host']}) se ha unido al clúster")
                    return
                if state in ("rechazada", "caducada", "desconocida"):
                    cl["state"] = {"rechazada": "rechazado en el nodo"}.get(state, "caducado")
                    self.event(f"{cl['name']}: emparejamiento {cl['state']}")
                    return
                time.sleep(1.5)
            cl["state"] = "caducado"
        except Exception as e:
            cl["state"], cl["detail"] = "error", str(e)[:200]
            self.event(f"{cl['name']}: no se pudo completar el emparejamiento ({e})")

    def snapshot(self):
        nodes = []
        for n in self.reg.all():
            i = n.info
            nodes.append({"id": n.id, "name": n.name, "host": n.host, "up": self.reg.is_up(n),
                          "backend": i.get("backend"), "cap_bytes": i.get("cap_bytes"),
                          "bw_gbps": i.get("bw_gbps"), "cpu": i.get("cpu"), "os": i.get("os"),
                          "ram_free": i.get("ram_free"), "gpu": i.get("gpu"), "net": n.net,
                          "rpc_bin": i.get("rpc_bin"), "rpc_build": i.get("rpc_build"),
                          "device": i.get("device"), "device_desc": i.get("device_desc"),
                          "devices": i.get("devices"), "update": i.get("update"),
                          "auto_update": i.get("auto_update"), "settings": self.node_settings(n.name),
                          "telemetry": i.get("telemetry"),
                          "rpc_running": i.get("rpc_running"),
                          "used": any(n.id in c.used for c in list(self.slots.values())),
                          "error": n.last_error, "cores": i.get("cores"), "threads": i.get("threads"),
                          "ram_total": i.get("ram_total"), "limits_mode": i.get("limits_mode"),
                          "limits": i.get("limits"), "mem_source": i.get("mem_source"),
                          "threads_source": i.get("threads_source"), "recommended": i.get("recommended"),
                          "ceiling": i.get("ceiling")})
        return {"version": __version__, "notice": legal_notice(), "multimodal": bool(self.mmproj and self.state == READY),
                "mmproj": self.mmproj, "state": self.state, "error": self.error, "model": self.model,
                "ctx": self.ctx, "plan": self.plan, "recoveries": self.recoveries, "nodes": nodes,
                "slots": self.slots_view(),
                "ledger": self.ledger.view({n.id: n.info.get("cap_bytes") or 0 for n in self.reg.all()}),
                "events": list(self.events)[-40:], "llama_log": list(self.logbuf)[-15:], "last_crash": self.last_crash,
                "llama_build": self.llama_build, "min_mbps": self.cfg.min_mbps, **self.pending_view()}

    def metrics(self):
        L = ["# HELP dllm_state Estado del orquestador (1 = estado actual)", "# TYPE dllm_state gauge"]
        for s in (IDLE, LOADING, READY, RECOVERING, ERROR):
            L.append(f'dllm_state{{state="{s}"}} {int(self.state == s)}')
        L += ["# TYPE dllm_node_up gauge", "# TYPE dllm_node_cap_bytes gauge", "# TYPE dllm_node_net_mbps gauge"]
        for n in self.reg.all():
            lab = f'node="{n.name}"'
            L.append(f"dllm_node_up{{{lab}}} {int(self.reg.is_up(n))}")
            L.append(f"dllm_node_cap_bytes{{{lab}}} {n.info.get('cap_bytes') or 0}")
            L.append(f"dllm_node_net_mbps{{{lab}}} {(n.net or {}).get('mbps', 0)}")
        L += ["# TYPE dllm_recoveries_total counter", f"dllm_recoveries_total {self.recoveries}",
              "# TYPE dllm_requests_total counter", f"dllm_requests_total {self.requests}",
              "# TYPE dllm_request_errors_total counter", f"dllm_request_errors_total {self.req_errors}"]
        return "\n".join(L) + "\n"

    def shutdown(self):
        for c in [c for k, c in list(self.slots.items()) if k and not self.parent]:
            c.dead = True
            c.shutdown()
        with self.lock:
            self.gen += 1
            self.desired = None
        self._teardown()


# ============================================================ HTTP
def extract_model(body, ctype=""):
    """Campo «model» de la petición: JSON o formulario multipart (p. ej. /v1/audio/transcriptions)."""
    if not body:
        return None
    try:
        if "multipart/form-data" in (ctype or ""):
            m = re.search(rb'name="model"\r\n\r\n([^\r\n]{1,200})', body[:1 << 20])
            return m.group(1).decode("utf-8", "replace") if m else None
        d = json.loads(body)
        v = d.get("model") if isinstance(d, dict) else None
        return v if isinstance(v, str) else None
    except ValueError:
        return None


def make_handler(cfg, cl):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dllm"

        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, raw=None, ctype="application/json", headers=None):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, str(v))
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            """Sin clave desde este mismo equipo (el navegador del orquestador); desde la red, API key."""
            if not cfg.api_key or self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return True
            return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + cfg.api_key)

        def _same_origin(self):
            """Contra CSRF / DNS rebinding: una web ajena abierta en el navegador no puede mandar órdenes al panel."""
            origin = self.headers.get("Origin")
            if not origin:  # curl, scripts, clientes de la API
                return True
            return urlparse(origin).netloc == self.headers.get("Host", "")

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def _route(self):
            path = urlparse(self.path).path
            try:
                if path == "/":
                    # pkgutil funciona también cuando el paquete va dentro de un .pyz
                    return self._send(200, raw=pkgutil.get_data("dllm", "ui.html"),
                                      ctype="text/html; charset=utf-8")
                if path == "/healthz":
                    return self._send(200, {"ok": True})
                ip = self.client_address[0]
                if cfg.allow_ip and ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and ip not in cfg.allow_ip:
                    self.close_connection = True
                    return self._send(403, {"error": "IP no permitida"})
                if self.command == "POST" and not self._same_origin():
                    return self._send(403, {"error": "origen no permitido"})
                if not self._authed():
                    self.close_connection = True
                    return self._send(401, {"error": "API key requerida (Authorization: Bearer ...)"})
                body = b""
                if self.command == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n) if n else b""
                if path.startswith("/v1/"):
                    return self._proxy(body)
                req = json.loads(body) if body else {}
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                mm = cl.models
                if path in ("/api/ui/ping", "/api/ui/bye") and self.command == "POST":
                    cid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                    (cl.presence.touch if path.endswith("ping") else cl.presence.bye)(cid)
                    return self._send(200, {"ok": True})
                if path == "/api/ui/clients":
                    return self._send(200, cl.presence.view())
                if path == "/api/pair/claim" and self.command == "POST":
                    try:
                        return self._send(200, cl.claim(str(json.loads(body or b"{}").get("host") or "")))
                    except (ValueError, OSError) as e:
                        return self._send(400, {"error": str(e)})
                if path == "/api/access":
                    local = self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                    out = {"local": local, "key_required": bool(cfg.api_key) and not local,
                           "exposed": cfg.host not in ("127.0.0.1", "localhost", "::1")}
                    if local and cfg.api_key:
                        out["lan_url"] = f"http://{config.lan_ip()}:{cfg.port}/#key={cfg.api_key}"
                    return self._send(200, out)
                if path == "/api/state":
                    return self._send(200, cl.snapshot())
                if path == "/api/models":
                    return self._send(200, cl.list_models())
                if path == "/metrics":
                    return self._send(200, raw=cl.metrics().encode(), ctype="text/plain; version=0.0.4")
                if path == "/api/plan" and self.command == "POST":
                    _, p = cl.make_plan(cl.resolve_model(req["model"]), int(req.get("ctx", 4096)),
                                        force_bench=False)
                    return self._send(200, p)
                if path == "/api/load" and self.command == "POST":
                    ctx = req.get("ctx") or mm.settings(req["model"]).get("ctx") or 4096
                    slot = cl.load_model(req["model"], int(ctx), req.get("slot", 0), req.get("pinned", True))
                    return self._send(202, {"accepted": True, "slot": slot})
                # ---- gestión de modelos
                if path == "/api/hf/search":
                    return self._send(200, models.hf_search(q.get("q"), mm.hf_token()))
                if path == "/api/hf/files":
                    return self._send(200, models.hf_files(q.get("repo"), mm.hf_token()))
                if path == "/api/hf/token":
                    if self.command == "POST":
                        mm.set_hf_token(req.get("token"))
                    return self._send(200, {"set": bool(mm.hf_token())})
                if path == "/api/ollama/local":
                    return self._send(200, {"dir": str(models.ollama_local_dir()), "models": models.ollama_local_list()})
                if path == "/api/downloads":
                    if self.command == "POST":
                        src = req.get("source")
                        if src == "hf":
                            job = mm.add_hf(req.get("repo"), req.get("file"))
                        elif src == "ollama":
                            job = mm.add_ollama(req.get("name"))
                        elif src == "ollama-local":
                            job = mm.add_ollama_local(req.get("name"))
                        else:
                            raise ValueError("origen desconocido (hf, ollama u ollama-local)")
                        cl.event(f"descarga en cola: {job['label']}")
                        return self._send(202, job)
                    return self._send(200, mm.views())
                if path == "/api/downloads/cancel" and self.command == "POST":
                    return self._send(200, {"ok": mm.cancel(req.get("id"))})
                if path == "/api/models/delete" and self.command == "POST":
                    cl.delete_model(req.get("model"))
                    return self._send(200, {"ok": True})
                if path == "/api/models/settings" and self.command == "POST":
                    return self._send(200, {"settings": mm.set_settings(req.get("model"), req.get("settings") or {})})
                if path == "/api/node/settings" and self.command == "POST":
                    return self._send(200, cl.set_node_settings(req))
                if path == "/api/telemetry":
                    return self._send(200, cl.telemetry())
                if path == "/api/node/limits" and self.command == "POST":
                    return self._send(200, cl.set_node_limits(req))
                if path == "/api/unload" and self.command == "POST":
                    if req.get("slot") in (None, 0, "0"):
                        cl.unload()
                    else:
                        cl.drop_slot(req["slot"])
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "no encontrado"})
            except Busy as e:
                self._send(409, {"error": str(e)})
            except (ValueError, KeyError, models.ModelError) as e:
                self._send(400, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log("error en", self.path, e)
                self._send(500, {"error": str(e)})

        def _proxy(self, body):
            """Enrutador L7: elige el slot por el campo «model» y la capacidad que exige la ruta."""
            cl.requests += 1
            path = urlparse(self.path).path
            if path == "/v1/models" and self.command == "GET":
                return self._send(200, cl.models_listing())
            try:
                c = cl.route(extract_model(body, self.headers.get("Content-Type", "")), engine.capability_of(path))
            except RouteError as e:
                cl.req_errors += 1
                return self._send(e.status, oai_error(e.status, str(e)),
                                  headers={"Retry-After": e.retry_after} if e.retry_after else None)
            with c.lock:
                c.inflight += 1
            c.last_used = time.time()
            try:
                self._forward(c, path, body)
            finally:
                with c.lock:
                    c.inflight -= 1
                c.last_used = time.time()

        def _forward(self, c, path, body):
            if body and path in ("/v1/chat/completions", "/v1/completions"):
                # parámetros por defecto del modelo (temperatura, prompt de sistema…) sin pisar los del cliente
                try:
                    st = cl.models.settings(c.model) if c.model else {}
                    body = json.dumps(models.apply_settings(path, json.loads(body), st)).encode()
                except ValueError:
                    pass  # cuerpo no JSON: que llama-server responda el error
            up = http.client.HTTPConnection("127.0.0.1", c.llama_port, timeout=3600)
            try:
                up.request(self.command, self.path, body=body or None,
                           headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
                r = up.getresponse()
            except OSError as e:
                cl.req_errors += 1
                up.close()
                return self._send(502, oai_error(502, f"llama-server no responde: {e}"))
            try:
                ctype = r.getheader("Content-Type", "application/json")
                if r.getheader("Content-Length") is not None:
                    return self._send(r.status, raw=r.read(), ctype=ctype)
                # streaming (SSE): reenvío troceado
                self.send_response(r.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while True:
                    chunk = r.read1(8192)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            finally:
                if r.status >= 500:
                    cl.req_errors += 1
                up.close()

    return H


def run(cfg):
    secret = cfg.secret
    if not secret:
        sys.exit("falta el secreto del clúster: ejecuta `dllm init` o usa --secret / DLLM_SECRET")
    loopback = cfg.host in ("127.0.0.1", "localhost", "::1")
    if cfg.api_key == "auto":
        cfg.api_key = auth.new_secret()[:24]
        log(f"API key generada: {cfg.api_key}")
    if not loopback and not cfg.api_key:
        sys.exit("escuchar fuera de loopback exige --api-key (o --api-key auto)")
    if not cfg.llama_bin:
        log("AVISO: llama-server no encontrado; podrás ver nodos pero no cargar modelos")

    reg = Registry(secret, cfg.node_timeout, cfg.probe_interval)
    for n in cfg.node or []:
        h, _, p = n.partition(":")
        reg.add(h, int(p or 8765), static=True)
    cl = Cluster(cfg, reg)
    for fn in (reg.probe_loop, cl.watchdog, cl.metrics_loop):
        threading.Thread(target=fn, daemon=True).start()
    threading.Thread(target=reg.beacon_loop, args=(cfg.beacon_port,), daemon=True).start()

    srv = FastHTTPServer((cfg.host, cfg.port), make_handler(cfg, cl))
    srv.daemon_threads = True
    log(legal_notice().replace("\n", " · "))
    log(f"DLLM {__version__} · UI y API en http://{cfg.host}:{cfg.port} · modelos: {cfg.models_dir}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("cerrando: deteniendo llama-server y rpc-servers")
        cl.shutdown()
