# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Alertas por webhook: los eventos (nodo caído, fallo del modelo, seguridad…) se envían por HTTP POST a las URLs
configuradas, firmados con HMAC-SHA256 para que el receptor compruebe que vienen de este orquestador.

Cabeceras: `X-DLLM-Event` (tipo), `X-DLLM-Timestamp` (epoch s) y `X-DLLM-Signature: sha256=<hex>` con
HMAC-SHA256(secreto, "<timestamp>.<cuerpo>"). Formatos: `generic` (JSON con el evento), `slack` ({"text"}, también
Mattermost/Rocket.Chat) y `discord` ({"content"}). Envío en segundo plano con reintentos; no sigue redirecciones.
Configuración en `~/.dllm/webhooks.json` (600)."""
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, i18n

LEVELS = ("info", "warn", "error")
FORMATS = ("generic", "slack", "discord")
KINDS = ("load", "unload", "node", "net", "security", "backup", "request", "route", "tools", "download", "test")
MAX_HOOKS = 20
PER_MINUTE = 30          # máximo de envíos por webhook y minuto (el resto se descarta y se cuenta)
_NAME = re.compile(r"^[\w .@-]{1,64}$")


class WebhookError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # una redirección podría llevar el evento a otro sitio: se trata como error


_opener = urllib.request.build_opener(_NoRedirect)


def valid_url(url):
    if not isinstance(url, str) or len(url) > 500 or any(c in url for c in "\r\n \t"):
        raise WebhookError("URL no válida")
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise WebhookError("la URL debe empezar por http:// o https:// y tener un servidor")
    if u.username or u.password:
        raise WebhookError("la URL no puede llevar usuario ni contraseña: usa el secreto de firma")
    return url


def sign(secret, ts, body):
    return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def payload(hook, ev, origin):
    level, msg = ev.get("level", "info"), i18n.tr(ev.get("msg", ""))
    icon = {"error": "🔴", "warn": "🟠"}.get(level, "🔵")
    text = f"{icon} DLLM ({origin}) · {level} · {ev.get('kind') or '-'}: {msg}"
    fmt = hook.get("format", "generic")
    if fmt == "slack":
        return {"text": text}
    if fmt == "discord":
        return {"content": text[:1900]}
    return {"source": "dllm", "version": __version__, "origin": origin,
            "event": {**{k: ev.get(k) for k in ("t", "level", "kind", "node", "slot", "detail") if ev.get(k) is not None},
                      "msg": msg}}


class Webhooks:
    def __init__(self, path=None, origin=None, timeout=5, retries=(1, 5)):
        self.path = Path(path) if path else None
        self.origin = origin or socket.gethostname()
        self.timeout, self.retries = timeout, retries
        self.lock = threading.Lock()
        self.hooks = self._load()
        self.status = {}   # id → {"t", "ok", "code", "error", "sent", "dropped"}
        self.recent = {}   # id → marcas de tiempo de envíos del último minuto
        self.q = queue.Queue(maxsize=500)
        self._thread = None

    # ------------------------------------------------------------------ persistencia
    def _load(self):
        if not self.path:
            return {}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d.get("hooks", {}) if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def reload(self):
        with self.lock:
            self.hooks = self._load()

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"hooks": self.hooks}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ gestión
    def view(self):
        with self.lock:
            return {"hooks": {i: {**{k: v for k, v in h.items() if k != "secret"},
                                  "secret_hint": h["secret"][:4] + "…", "status": self.status.get(i)}
                              for i, h in self.hooks.items()},
                    "levels": list(LEVELS), "formats": list(FORMATS), "kinds": list(KINDS)}

    def set(self, req):
        """Crea (sin id) o modifica un webhook. Devuelve (id, secreto si es nuevo o se regenera, si no None)."""
        if not isinstance(req, dict):
            raise WebhookError("petición no válida")
        with self.lock:
            hid = req.get("id")
            if hid is not None and hid not in self.hooks:
                raise WebhookError("webhook no encontrado")
            if hid is None and len(self.hooks) >= MAX_HOOKS:
                raise WebhookError(f"máximo {MAX_HOOKS} webhooks")
            h = dict(self.hooks.get(hid) or {"enabled": True, "format": "generic", "min_level": "warn", "kinds": []})
            if "name" in req or hid is None:
                name = str(req.get("name") or "").strip() or "webhook"
                if not _NAME.match(name):
                    raise WebhookError("nombre no válido (máx. 64: letras, números, espacio . _ @ -)")
                h["name"] = name
            if "url" in req or hid is None:
                h["url"] = valid_url(req.get("url"))
            if "format" in req:
                if req["format"] not in FORMATS:
                    raise WebhookError("formato no válido: " + ", ".join(FORMATS))
                h["format"] = req["format"]
            if "min_level" in req:
                if req["min_level"] not in LEVELS:
                    raise WebhookError("nivel no válido: " + ", ".join(LEVELS))
                h["min_level"] = req["min_level"]
            if "kinds" in req:
                ks = req["kinds"]
                if isinstance(ks, str):
                    ks = [k.strip() for k in ks.split(",") if k.strip()]
                if not isinstance(ks, list) or any(k not in KINDS for k in ks):
                    raise WebhookError("tipos no válidos: " + ", ".join(KINDS))
                h["kinds"] = sorted(set(ks))
            if "enabled" in req:
                h["enabled"] = bool(req["enabled"])
            new_secret = None
            if hid is None or req.get("regenerate"):
                new_secret = h["secret"] = secrets.token_urlsafe(32)
            hid = hid or secrets.token_hex(4)
            self.hooks[hid] = h
            self.save()
        return hid, new_secret

    def delete(self, hid):
        with self.lock:
            if self.hooks.pop(hid, None) is None:
                raise WebhookError("webhook no encontrado")
            self.status.pop(hid, None)
            self.save()

    # ------------------------------------------------------------------ envío
    def matches(self, h, ev):
        if not h.get("enabled", True):
            return False
        if ev.get("kind") == "test":
            return True
        if LEVELS.index(ev.get("level", "info")) < LEVELS.index(h.get("min_level", "warn")):
            return False
        return not h.get("kinds") or ev.get("kind") in h["kinds"]

    def emit(self, ev):
        """Encola el evento para cada webhook que lo quiera (no bloquea)."""
        with self.lock:
            targets = [i for i, h in self.hooks.items() if self.matches(h, ev)]
        if not targets:
            return
        self._start()
        now = time.time()
        for i in targets:
            with self.lock:
                r = [t for t in self.recent.get(i, []) if now - t < 60]
                st = self.status.setdefault(i, {"sent": 0, "dropped": 0})
                if len(r) >= PER_MINUTE:
                    st["dropped"] = st.get("dropped", 0) + 1
                    continue
                r.append(now)
                self.recent[i] = r
            try:
                self.q.put_nowait((i, ev))
            except queue.Full:
                with self.lock:
                    st["dropped"] = st.get("dropped", 0) + 1

    def _start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, daemon=True, name="webhooks")
        self._thread.start()

    def _worker(self):
        while True:
            hid, ev = self.q.get()
            for wait in (0,) + tuple(self.retries):
                if wait:
                    time.sleep(wait)
                r = self.send(hid, ev)
                if r.get("ok") or (r.get("code") and 400 <= r["code"] < 500 and r["code"] != 429):
                    break  # entregado, o error del receptor que no se arregla reintentando

    def send(self, hid, ev):
        """Envía un evento a un webhook ahora mismo y devuelve el resultado (también lo usa «Probar»)."""
        with self.lock:
            h = dict(self.hooks.get(hid) or {})
        if not h:
            raise WebhookError("webhook no encontrado")
        body = json.dumps(payload(h, ev, self.origin), ensure_ascii=False).encode()
        ts = str(int(time.time()))
        req = urllib.request.Request(h["url"], data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": f"DLLM/{__version__}",
            "X-DLLM-Event": ev.get("kind") or "event", "X-DLLM-Timestamp": ts,
            "X-DLLM-Signature": sign(h["secret"], ts, body)})
        res = {"t": time.time(), "ok": False, "code": None, "error": ""}
        try:
            with _opener.open(req, timeout=self.timeout) as r:
                res["code"] = r.status
                res["ok"] = 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            res["code"], res["error"] = e.code, f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            res["error"] = str(getattr(e, "reason", e))[:200]
        with self.lock:
            st = self.status.setdefault(hid, {"sent": 0, "dropped": 0})
            st.update(res)
            if res["ok"]:
                st["sent"] = st.get("sent", 0) + 1
        return res

    def test(self, hid):
        return self.send(hid, {"t": round(time.time(), 3), "level": "info", "kind": "test",
                               "msg": "prueba de webhook desde DLLM"})
