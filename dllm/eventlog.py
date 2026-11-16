# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Registro de eventos estructurado y métricas por modelo.

- `EventLog`: cada evento lleva hora, nivel (info/warn/error), modelo (slot), nodo y tipo. Se guarda en memoria (consulta
  con filtros desde el panel y `/api/events`) y en `events.jsonl` (sobrevive a reinicios; rota por tamaño).
- `SlotMetrics`: contadores y latencias de las peticiones de un modelo (total, errores por código, media, p50, p95)."""
import collections
import json
import os
import re
import threading
import time

LEVELS = {"info": 0, "warn": 1, "error": 2}
_ERR = re.compile(r"^(\[slot \d+\] )?(error|fallo)\b|terminó \(código|no cabe|no se pudo", re.I)
_WARN = re.compile(r"^(\[slot \d+\] )?aviso\b|caído|sin respuesta|reintentando|replanificando|no disponible", re.I)


def classify(msg):
    """Nivel de un mensaje libre (los eventos antiguos no lo indican)."""
    if _ERR.search(msg):
        return "error"
    if _WARN.search(msg):
        return "warn"
    return "info"


class EventLog:
    def __init__(self, path=None, maxlen=2000, max_bytes=2 << 20, keep=3):
        self.path, self.max_bytes, self.keep = path, max_bytes, keep
        self.mem = collections.deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.seq = 0
        if path:
            self._preload()

    def _preload(self, n=500):
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in collections.deque(f, maxlen=n):
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    ev["old"] = True  # de una ejecución anterior
                    self._push(ev)
        except OSError:
            pass

    def _push(self, ev):
        self.seq += 1
        ev["id"] = self.seq
        self.mem.append(ev)

    def add(self, msg, level=None, slot=0, node=None, kind=None, detail=None):
        ev = {"t": round(time.time(), 3), "level": level or classify(msg), "slot": slot, "node": node,
              "kind": kind, "msg": msg}
        if detail:
            ev["detail"] = detail
        with self.lock:
            last = self.mem[-1] if self.mem else None
            if last and last["msg"] == msg and last["level"] == ev["level"] and last.get("slot") == slot \
                    and ev["t"] - last["t"] < 5:
                last["count"] = last.get("count", 1) + 1  # mismo evento repetido: se cuenta, no se duplica
                return last
            self._push(ev)
            if self.path:
                self._write(ev)
        return ev

    def _write(self, ev):
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                last = f"{self.path}.{self.keep}"
                if os.path.exists(last):
                    os.remove(last)
                for i in range(self.keep - 1, 0, -1):
                    if os.path.exists(f"{self.path}.{i}"):
                        os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
                os.replace(self.path, f"{self.path}.1")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass  # el registro nunca debe tumbar el programa

    def query(self, level=None, slot=None, node=None, q=None, since=None, limit=300):
        """Eventos más recientes que cumplen los filtros (el más antiguo primero). `level`: nivel mínimo."""
        mn = LEVELS.get(level or "info", 0)
        q = (q or "").lower()
        out = []
        with self.lock:
            items = list(self.mem)
        for ev in items:
            if LEVELS.get(ev.get("level"), 0) < mn:
                continue
            if slot is not None and ev.get("slot") != slot:
                continue
            if node and ev.get("node") != node:
                continue
            if since and ev["t"] <= since:
                continue
            if q and q not in ev["msg"].lower() and q not in json.dumps(ev.get("detail") or "").lower():
                continue
            out.append(ev)
        return out[-max(1, min(int(limit), 2000)):]

    def counts(self):
        c = {"info": 0, "warn": 0, "error": 0}
        with self.lock:
            for ev in self.mem:
                c[ev.get("level", "info")] = c.get(ev.get("level", "info"), 0) + 1
        return c


class SlotMetrics:
    """Peticiones de un modelo: contadores por código HTTP y latencia de las últimas `keep`."""

    def __init__(self, keep=300):
        self.lock = threading.Lock()
        self.total, self.errors = 0, 0
        self.by_status = collections.Counter()
        self.lat = collections.deque(maxlen=keep)  # segundos
        self.last_error = None

    def record(self, status, seconds, error=None):
        with self.lock:
            self.total += 1
            self.by_status[int(status)] += 1
            self.lat.append(float(seconds))
            if status >= 500 or status == 429:
                self.errors += 1
                self.last_error = {"t": round(time.time(), 3), "status": int(status), "msg": error or ""}

    @staticmethod
    def _pct(sorted_vals, p):
        if not sorted_vals:
            return None
        return sorted_vals[min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))]

    def summary(self):
        with self.lock:
            v = sorted(self.lat)
            ms = lambda x: None if x is None else round(x * 1000, 1)
            return {"requests": self.total, "errors": self.errors, "by_status": dict(self.by_status),
                    "avg_ms": ms(sum(v) / len(v)) if v else None, "p50_ms": ms(self._pct(v, 0.5)),
                    "p95_ms": ms(self._pct(v, 0.95)), "last_error": self.last_error}
