# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Cuánto aporta un nodo: memoria (RAM o VRAM) e hilos de CPU.

Dos modos, elegidos al instalar el nodo:
  - auto:   el orquestador decide (por defecto el valor recomendado) y se puede cambiar desde su panel web.
  - manual: los límites se fijan en el propio nodo; el orquestador solo puede leerlos.

Cómo se hacen cumplir:
  - Hilos: rpc-server recibe `-t N`, así que nunca usa más de N hilos de CPU.
  - Memoria: rpc-server no tiene opción para limitarla, así que el límite lo cumple el planificador: nunca asigna a
    un nodo más capas (pesos + caché KV) de las que caben en lo que ofrece.
  - GPU: se limita su VRAM utilizable; el % de cómputo de una GPU no se puede limitar.
"""
GiB = 1 << 30
# como mucho este % del total, y nunca más de lo libre menos la reserva. La RAM la comparte el sistema; la VRAM
# de una GPU dedicada está para esto, así que en GPU manda lo libre menos la reserva.
RECOMMENDED_FRACTION = 0.75
RECOMMENDED_FRACTION_GPU = 0.95


def pool(hw):
    """(total, libre) de la memoria que se presta: VRAM si el nodo usa GPU, si no RAM."""
    g = hw.get("gpu")
    if g:
        return g["total"], g["free"]
    return hw["ram_total"], hw["ram_free"]


def recommend(hw):
    total, free = pool(hw)
    frac = RECOMMENDED_FRACTION_GPU if hw.get("gpu") else RECOMMENDED_FRACTION
    mem = min(free - hw["reserve"], int(total * frac))
    # cores//2 ≈ núcleos físicos: es lo que mejor rinde en llama.cpp y lo que usa rpc-server por defecto
    return {"mem_bytes": max(0, int(mem)), "threads": max(1, hw["cores"] // 2)}


def resolve(limits, hw):
    """limits: {share (1-100), mem_gb, threads}; None = sin fijar. Prioridad: valor explícito > porcentaje >
    recomendado. Un valor fijado se respeta mientras no supere el total menos la reserva."""
    limits = limits or {}
    total, _ = pool(hw)
    rec = recommend(hw)
    ceiling = max(0, total - hw["reserve"])
    share = limits.get("share")
    if limits.get("mem_gb") is not None:
        mem, mem_src = int(float(limits["mem_gb"]) * GiB), "fijo"
    elif share:
        mem, mem_src = int(total * share / 100), f"{share:g} %"
    else:
        mem, mem_src = rec["mem_bytes"], "recomendado"
    if limits.get("threads"):
        threads, thr_src = int(limits["threads"]), "fijo"
    elif share:
        threads, thr_src = round(hw["cores"] * share / 100), f"{share:g} %"
    else:
        threads, thr_src = rec["threads"], "recomendado"
    if mem > ceiling:
        mem_src += " (recortado al máximo)"
    if threads > hw["cores"]:
        thr_src += " (recortado al máximo)"
    return {"cap_bytes": max(0, min(mem, ceiling)), "threads": max(1, min(threads, hw["cores"])),
            "mem_source": mem_src, "threads_source": thr_src, "recommended": rec, "ceiling": ceiling}


def validate(body, hw):
    """Valida una petición de cambio. Devuelve los límites nuevos (dict) o lanza ValueError con mensaje claro."""
    if body.get("reset"):
        return {"share": None, "mem_gb": None, "threads": None}
    total, _ = pool(hw)
    ceiling_gb = max(0, total - hw["reserve"]) / GiB
    out = {}
    for k in ("share", "mem_gb", "threads"):
        v = body.get(k)
        if v in (None, ""):
            out[k] = None
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"'{k}' debe ser un número")
        if k == "share" and not 1 <= v <= 100:
            raise ValueError("el porcentaje debe estar entre 1 y 100")
        if k == "mem_gb" and not 0.25 <= v <= ceiling_gb + 1e-9:
            raise ValueError(f"la memoria debe estar entre 0,25 y {ceiling_gb:.1f} GiB (total menos la reserva)")
        if k == "threads":
            if v != int(v) or not 1 <= v <= hw["cores"]:
                raise ValueError(f"los hilos deben ser un entero entre 1 y {hw['cores']}")
            v = int(v)
        out[k] = v
    return out
