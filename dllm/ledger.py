# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Libro mayor de memoria (VRAM + RAM): cuánto tiene reservado cada slot de modelo en cada nodo.

Dos dimensiones por nodo:
- `vram`: el aporte del nodo (`cap_bytes`, el límite que fija la persona) en nodos con GPU.
- `ram`: la RAM del nodo. En nodos solo-CPU el aporte (`cap_bytes`) ES RAM y las reservas de pesos y caché van aquí;
  en nodos con GPU la RAM solo cubre los buffers de E/S. El propio equipo del orquestador figura como `HOST`
  (modelos `cpu_only` o la parte en CPU de `gpu_with_cpu_fallback`).

Disponible para un slot nuevo = presupuesto − márgenes de seguridad − Σ reservas de los OTROS slots. El margen de VRAM
solo se resta si otro slot ya ocupa el nodo (el planificador ya deja su propio margen por nodo para el primero); el de
RAM se resta siempre, para proteger al sistema operativo y evitar swap. La RAM libre real (`MemAvailable`, `vm_stat`,
`GlobalMemoryStatusEx`) se usa como tope adicional. `plan_lock` serializa «planificar + reservar» entre slots: sin él,
dos cargas simultáneas verían la misma memoria.
"""
import threading

MB = 1 << 20
VRAM_MARGIN = 256 * MB
RAM_MARGIN = 1024 * MB
HOST = "@host"  # el equipo del orquestador


class Ledger:
    def __init__(self, vram_margin=VRAM_MARGIN, ram_margin=RAM_MARGIN):
        self.vram_margin, self.ram_margin = int(vram_margin), int(ram_margin)
        self.res = {}                 # slot → {"vram": {nodo: bytes}, "ram": {nodo: bytes}}
        self.lock = threading.Lock()
        self.plan_lock = threading.RLock()

    def reserve(self, slot, vram=None, ram=None):
        with self.lock:
            self.res[slot] = {"vram": {k: int(v) for k, v in (vram or {}).items()},
                              "ram": {k: int(v) for k, v in (ram or {}).items()}}

    def release(self, slot):
        with self.lock:
            self.res.pop(slot, None)

    @staticmethod
    def _skip(exclude):
        return set(exclude) if isinstance(exclude, (set, frozenset, list, tuple)) else {exclude}

    def reserved(self, node_id, dim="vram", exclude=None):
        skip = self._skip(exclude)
        with self.lock:
            return sum(r[dim].get(node_id, 0) for s, r in self.res.items() if s not in skip)

    def available_pool(self, node_id, cap_bytes, dim="vram", exclude=None):
        """Aporte del nodo (`cap_bytes`) menos lo reservado por otros slots en esa dimensión; el margen solo se
        resta si ya hay reservas (el planificador deja su propio margen por nodo para el primer slot)."""
        used = self.reserved(node_id, dim, exclude)
        return max(0, int(cap_bytes) - used - ((self.vram_margin if dim == "vram" else self.ram_margin) if used else 0))

    def available_vram(self, node_id, cap_bytes, exclude=None):
        return self.available_pool(node_id, cap_bytes, "vram", exclude)

    def available_ram(self, node_id, budget_bytes, live_free=None, exclude=None):
        """budget_bytes: RAM que el nodo presta (aporte o RAM total); live_free: RAM libre medida ahora (opcional)."""
        free = int(budget_bytes) - self.reserved(node_id, "ram", exclude) - self.ram_margin
        if live_free is not None:
            free = min(free, int(live_free) - self.ram_margin)
        return max(0, free)

    # compatibilidad con la 1.ª versión del libro (solo VRAM)
    available = available_vram

    def view(self, caps):
        """caps: {nodo: cap_bytes} → desglose por nodo para el panel y las pruebas."""
        out = {}
        with self.lock:
            for nid, cap in caps.items():
                vr = {s: r["vram"][nid] for s, r in self.res.items() if nid in r["vram"]}
                rr = {s: r["ram"][nid] for s, r in self.res.items() if nid in r["ram"]}
                used = sum(vr.values())
                out[nid] = {"cap": int(cap), "reserved": vr, "used": used,
                            "free": max(0, int(cap) - used - (self.vram_margin if used else 0)),
                            "ram_reserved": rr}
            host = {s: r["ram"][HOST] for s, r in self.res.items() if HOST in r["ram"]}
            if host:
                out[HOST] = {"cap": 0, "reserved": {}, "used": 0, "free": 0, "ram_reserved": host}
        return out
