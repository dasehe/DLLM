# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Registro de auditoría: cada acción de administración (quién, cuándo, desde qué IP, con qué credencial, qué ruta,
con qué parámetros y con qué resultado). Fichero propio `audit.jsonl` (permisos 600), solo se añade, rota por tamaño.

Los parámetros se guardan resumidos y sin secretos (contraseñas, claves, tokens, frases de paso, contenido de
copias de seguridad)."""
import json
import os
import re
import threading
import time

_SECRET = re.compile(r"pass|secret|token|key|phrase|data|content|blob|^(old|new)$", re.I)
_KEEP_KEY = ("key_required",)  # nombres que contienen «key» pero no son secretos


def sanitize(v, depth=0):
    """Copia resumida de los parámetros de una petición, sin secretos."""
    if depth > 3:
        return "…"
    if isinstance(v, dict):
        out = {}
        for i, (k, x) in enumerate(v.items()):
            if i >= 30:
                out["…"] = len(v) - 30
                break
            k = str(k)[:64]
            out[k] = "***" if _SECRET.search(k) and k not in _KEEP_KEY and x not in (None, "", False) \
                else sanitize(x, depth + 1)
        return out
    if isinstance(v, list):
        return [sanitize(x, depth + 1) for x in v[:20]] + (["…"] if len(v) > 20 else [])
    if isinstance(v, str):
        return v if len(v) <= 200 else v[:200] + "…"
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return str(v)[:200]


class Audit:
    def __init__(self, path=None, max_bytes=5 << 20, keep=5):
        self.path, self.max_bytes, self.keep = path, max_bytes, keep
        self.lock = threading.Lock()
        self.mem = []  # sin fichero (pruebas): en memoria

    def record(self, action, user=None, via=None, ip=None, status=None, params=None, detail=None):
        e = {"t": round(time.time(), 3), "user": user, "via": via, "ip": ip, "action": str(action)[:200],
             "status": status, "ok": status is not None and status < 400}
        if params:
            e["params"] = sanitize(params)
        if detail:
            e["detail"] = str(detail)[:300]
        with self.lock:
            if not self.path:
                self.mem.append(e)
                del self.mem[:-5000]
                return e
            try:
                self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except OSError:
                pass
        return e

    def _rotate(self):
        if not os.path.exists(self.path) or os.path.getsize(self.path) <= self.max_bytes:
            return
        last = f"{self.path}.{self.keep}"
        if os.path.exists(last):
            os.remove(last)
        for i in range(self.keep - 1, 0, -1):
            if os.path.exists(f"{self.path}.{i}"):
                os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
        os.replace(self.path, f"{self.path}.1")

    def _lines(self):
        """Entradas de la más reciente a la más antigua (fichero actual y rotados)."""
        if not self.path:
            yield from reversed(self.mem)
            return
        for p in [self.path] + [f"{self.path}.{i}" for i in range(1, self.keep + 1)]:
            try:
                with open(p, encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def query(self, limit=200, user=None, text=None, since=None, failed=False):
        limit = max(1, min(int(limit or 200), 2000))
        text = (text or "").lower()[:100]
        out = []
        for e in self._lines():
            if since is not None and e.get("t", 0) < since:
                break
            if user and e.get("user") != user:
                continue
            if failed and e.get("ok"):
                continue
            if text and text not in json.dumps(e, ensure_ascii=False).lower():
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out
