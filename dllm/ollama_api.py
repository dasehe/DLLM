# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""API compatible con Ollama (puerto propio, 11434 por defecto) para usar DLLM desde clientes que solo hablan Ollama.

Traduce /api/chat, /api/generate, /api/embed(dings), /api/tags, /api/show, /api/ps, /api/version y /api/pull al
proxy OpenAI del propio orquestador (/v1/*), así se reutilizan el enrutado por modelo, los parámetros por modelo, la
cola y las métricas. Mismas reglas de seguridad: fuera de loopback exige la API key (Authorization: Bearer) y rechaza
peticiones de navegador de otro origen. Las respuestas en streaming son NDJSON, como Ollama."""
import hashlib
import http.client
import json
import ssl
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from . import i18n
from .util import log

VERSION = "0.6.0"  # versión de Ollama cuyo protocolo se imita (los clientes la comprueban)
MAX_BODY = 64 << 20
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

# opciones de Ollama → parámetros OpenAI / llama-server
OPTIONS = {"temperature": "temperature", "top_p": "top_p", "top_k": "top_k", "min_p": "min_p", "typical_p": "typical_p",
           "seed": "seed", "stop": "stop", "repeat_penalty": "repeat_penalty", "presence_penalty": "presence_penalty",
           "frequency_penalty": "frequency_penalty"}


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(name):
    """Identificador estable para los clientes (no es el sha256 del fichero: DLLM no lo recalcula en cada listado)."""
    return hashlib.sha256(name.encode()).hexdigest()


def options_to_oai(opts):
    out = {}
    for k, v in (opts or {}).items():
        if k in OPTIONS and v is not None:
            out[OPTIONS[k]] = v
        elif k == "num_predict" and isinstance(v, int) and v > 0:
            out["max_tokens"] = v
    return out


def format_to_oai(fmt):
    if fmt == "json":
        return {"type": "json_object"}
    if isinstance(fmt, dict):
        return {"type": "json_schema", "json_schema": {"name": "respuesta", "schema": fmt}}
    return None


def _image_url(b):
    b = str(b)
    return b if b.startswith("data:") else "data:image/png;base64," + b


def messages_to_oai(msgs):
    if not isinstance(msgs, list):
        raise ApiError(400, "'messages' debe ser una lista")
    out = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            raise ApiError(400, "cada mensaje debe ser un objeto")
        role, content = m.get("role") or "user", m.get("content") or ""
        if m.get("images"):
            parts = [{"type": "text", "text": content}] if content else []
            parts += [{"type": "image_url", "image_url": {"url": _image_url(b)}} for b in m["images"]]
            content = parts
        om = {"role": role, "content": content}
        if m.get("tool_calls"):
            om["tool_calls"] = []
            for j, tc in enumerate(m["tool_calls"]):
                f = (tc or {}).get("function") or {}
                args = f.get("arguments")
                om["tool_calls"].append({"id": tc.get("id") or f"call_{i}_{j}", "type": "function",
                                         "function": {"name": f.get("name") or "",
                                                      "arguments": args if isinstance(args, str) else
                                                      json.dumps(args or {}, ensure_ascii=False)}})
        if role == "tool":
            om["tool_call_id"] = m.get("tool_call_id") or m.get("tool_name") or "call_0"
        out.append(om)
    return out


def tool_calls_from_oai(tcs):
    """OpenAI (argumentos en texto JSON) → Ollama (argumentos como objeto)."""
    out = []
    for tc in tcs or []:
        f = tc.get("function") or {}
        args = f.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"_raw": args}
        out.append({"function": {"name": f.get("name") or "", "arguments": args or {}}})
    return out


def make_handler(cfg, cl):
    up_host = "127.0.0.1" if cfg.host in ("0.0.0.0", "", "127.0.0.1", "localhost", "::", "::1") else cfg.host

    def upstream(path, body):
        t = getattr(cl, "tls", None)
        if t and up_host != "127.0.0.1":  # el orquestador escucha en una IP concreta con HTTPS: hablarle en HTTPS
            ctx = ssl.create_default_context(cafile=t["cert"])
            ctx.check_hostname = False  # es este mismo equipo; se verifica que el certificado sea el nuestro
            c = http.client.HTTPSConnection(up_host, cfg.port, timeout=3600, context=ctx)
        else:
            c = http.client.HTTPConnection(up_host, cfg.port, timeout=3600)
        headers = {"Content-Type": "application/json", "X-DLLM-Lang": "en"}
        if cfg.api_key:
            headers["Authorization"] = "Bearer " + cfg.api_key
        c.request("POST", path, body=json.dumps(body).encode(), headers=headers)
        return c, c.getresponse()

    def known_models():
        return [d["id"] for d in cl.models_listing()["data"]]

    def resolve(name):
        """Nombre que envía el cliente → nombre del modelo en DLLM (acepta «:latest» y nombres de Ollama)."""
        name = str(name or "").strip()
        if not name:
            return name
        known = known_models()
        if name in known:
            return name
        cands = [name[:-7]] if name.endswith(":latest") else []
        if ":" in name and "/" not in name:  # qwen2.5:7b → ollama/qwen2.5-7b.gguf (como lo guarda DLLM al descargar)
            model, tag = name.rsplit(":", 1)
            cands += [f"ollama/{model}-{tag}.gguf", f"ollama/{model.replace('/', '-')}-{tag}.gguf"]
        elif "/" not in name:
            cands.append(f"ollama/{name}-latest.gguf")
        for c in cands:
            if c in known:
                return c
        return name  # el enrutador de DLLM responde con el modelo principal o con el error adecuado

    def model_info(name):
        it = cl.models.list_one(name) if name else {}
        caps = {"completion"}
        for d in cl.models_listing()["data"]:
            if d["id"] == name:
                cs = set(((d.get("dllm") or {}).get("capabilities")) or [])
                if "embeddings" in cs:
                    caps = {"embedding"}
                caps |= {c for c in ("tools", "vision") if c in cs}
        if it.get("tool_template"):
            caps.add("tools")
        return it, sorted(caps)

    def details(it):
        arch = it.get("arch") or it.get("architecture") or ""
        return {"parent_model": "", "format": "gguf", "family": arch, "families": [arch] if arch else [],
                "parameter_size": it.get("params") or "", "quantization_level": it.get("quant") or ""}

    class H(BaseHTTPRequestHandler):
        server_version = "DLLM-Ollama"

        def log_message(self, *a):
            pass

        # ------------------------------------------------------------ utilidades HTTP
        def _json(self, code, obj):
            if code >= 400 and isinstance(obj, dict) and "error" in obj:
                obj = i18n.tr_error(obj, i18n.request_lang(self.headers, urlparse(self.path).query))
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _start_stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True

        def _line(self, obj):
            self.wfile.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
            self.wfile.flush()

        def _authed(self):
            """Desde este equipo, sin clave (como Ollama); desde la red, API key de un usuario con permiso «chat»."""
            if self.client_address[0] in LOOPBACK:
                return True
            authz = self.headers.get("Authorization", "")
            um = getattr(cl, "users", None)
            if not um or not authz.startswith("Bearer "):
                return False
            user, kid = um.key_user(authz[7:].strip())
            if not user or "chat" not in um.perms_of(user):
                return False
            ok, wait = um.allow(kid)
            if not ok:
                raise ApiError(429, f"límite de peticiones de esta API key superado: inténtalo dentro de {wait} s")
            return True

        def _same_origin(self):
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host", "")

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "petición demasiado grande")
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                raise ApiError(400, "el cuerpo debe ser JSON")

        # ------------------------------------------------------------ rutas
        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def do_DELETE(self):
            self._route()

        def _route(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path == "/":
                    body = b"Ollama is running (DLLM)"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                if self.command == "POST" and not self._same_origin():
                    raise ApiError(403, "origen no permitido")
                if not self._authed():
                    raise ApiError(401, "API key requerida (Authorization: Bearer ...)")
                if path == "/api/version":
                    return self._json(200, {"version": VERSION})
                if path == "/api/tags":
                    return self._json(200, {"models": self.tags()})
                if path == "/api/ps":
                    return self._json(200, {"models": self.ps()})
                if self.command not in ("POST", "DELETE"):
                    raise ApiError(405, "método no permitido")
                req = self._body()
                if path == "/api/show":
                    return self._json(200, self.show(req))
                if path == "/api/chat":
                    return self.chat(req)
                if path == "/api/generate":
                    return self.generate(req)
                if path == "/api/embed":
                    return self.embed(req, legacy=False)
                if path == "/api/embeddings":
                    return self.embed(req, legacy=True)
                if path == "/api/pull":
                    return self.pull(req)
                if path in ("/api/create", "/api/copy", "/api/delete", "/api/push") or path.startswith("/api/blobs"):
                    raise ApiError(501, "DLLM no admite esta operación de Ollama: gestiona los modelos desde el panel")
                raise ApiError(404, "ruta no encontrada")
            except ApiError as e:
                return self._json(e.status, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as e:  # nunca dejar al cliente sin respuesta
                log(f"API Ollama: {type(e).__name__}: {e}")
                try:
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
                except OSError:
                    pass

        # ------------------------------------------------------------ catálogo
        def tags(self):
            out = []
            by_name = {it["name"]: it for it in cl.list_models()}
            for name in known_models():
                it = by_name.get(name) or cl.models.list_one(name)
                mtime = it.get("mtime")
                out.append({"name": name, "model": name, "size": int(it.get("size") or 0), "digest": digest(name),
                            "modified_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat().replace("+00:00", "Z")
                            if mtime else now_iso(), "details": details(it)})
            return out

        def ps(self):
            out = []
            for k in sorted(cl.slots):
                c = cl.slots[k]
                if c.model and c.state == "READY":
                    it = cl.models.list_one(c.model)
                    out.append({"name": c.model, "model": c.model, "size": int(it.get("size") or 0),
                                "digest": digest(c.model), "details": details(it),
                                "expires_at": "2099-01-01T00:00:00Z", "size_vram": int(it.get("size") or 0),
                                "context_length": c.ctx})
            return out

        def show(self, req):
            name = resolve(req.get("model") or req.get("name"))
            if name not in known_models():
                raise ApiError(404, f"modelo '{name}' no encontrado")
            it, caps = model_info(name)
            sets = cl.models.settings(name)
            params = "\n".join(f"{k} {v}" for k, v in sets.items()
                               if k in OPTIONS or k in ("max_tokens", "system_prompt"))
            arch = it.get("arch") or ""
            info = {"general.architecture": arch, "general.basename": name}
            if it.get("ctx_train") or it.get("ctx"):
                info[f"{arch or 'model'}.context_length"] = it.get("ctx_train") or it.get("ctx")
            return {"modelfile": f"# DLLM\nFROM {name}\n", "parameters": params, "template": it.get("chat_template") or "",
                    "details": details(it), "model_info": info, "capabilities": caps, "modified_at": now_iso()}

        # ------------------------------------------------------------ inferencia
        def _oai_request(self, req, messages=None, prompt=None):
            name = resolve(req.get("model"))
            body = {"model": name, "stream": req.get("stream", True) is not False}
            body.update(options_to_oai(req.get("options")))
            fmt = format_to_oai(req.get("format"))
            if fmt:
                body["response_format"] = fmt
            if messages is not None:
                body["messages"] = messages
                if req.get("tools"):
                    body["tools"] = req["tools"]
            else:
                body["prompt"] = prompt
            if body["stream"]:
                body["stream_options"] = {"include_usage": True}
            return name, body

        def _final(self, name, t0, usage, reason, key, extra=None):
            usage = usage or {}
            d = {"model": name, "created_at": now_iso(), "done": True, "done_reason": reason or "stop",
                 "total_duration": int((time.time() - t0) * 1e9), "load_duration": 0,
                 "prompt_eval_count": usage.get("prompt_tokens") or 0, "prompt_eval_duration": 0,
                 "eval_count": usage.get("completion_tokens") or 0, "eval_duration": int((time.time() - t0) * 1e9)}
            if key == "message":
                d["message"] = dict({"role": "assistant", "content": ""}, **(extra or {}))
            else:
                d["response"] = ""
            return d

        def _run(self, path, name, body, key):
            t0 = time.time()
            conn, r = upstream(path, body)
            try:
                if r.status >= 400:
                    raw = r.read()
                    try:
                        e = json.loads(raw)
                        msg = (e.get("error") or {}).get("message") if isinstance(e.get("error"), dict) else e.get("error")
                    except ValueError:
                        msg = raw.decode("utf-8", "replace")[:300]
                    raise ApiError(r.status, msg or f"HTTP {r.status}")
                if not body["stream"]:
                    j = json.loads(r.read())
                    ch = (j.get("choices") or [{}])[0]
                    if key == "message":
                        m = ch.get("message") or {}
                        extra = {"content": m.get("content") or ""}
                        if m.get("tool_calls"):
                            extra["tool_calls"] = tool_calls_from_oai(m["tool_calls"])
                        out = self._final(name, t0, j.get("usage"), ch.get("finish_reason"), key, extra)
                    else:
                        out = self._final(name, t0, j.get("usage"), ch.get("finish_reason"), key)
                        out["response"] = ch.get("text") or ""
                    return self._json(200, out)
                self._start_stream()
                usage, reason, calls = None, None, {}
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        j = json.loads(data)
                    except ValueError:
                        continue
                    usage = j.get("usage") or usage
                    for ch in j.get("choices") or []:
                        reason = ch.get("finish_reason") or reason
                        if key == "message":
                            delta = ch.get("delta") or {}
                            for tc in delta.get("tool_calls") or []:
                                acc = calls.setdefault(tc.get("index", 0), {"function": {"name": "", "arguments": ""}})
                                f = tc.get("function") or {}
                                acc["function"]["name"] += f.get("name") or ""
                                acc["function"]["arguments"] += f.get("arguments") or ""
                            if delta.get("content"):
                                self._line({"model": name, "created_at": now_iso(), "done": False,
                                            "message": {"role": "assistant", "content": delta["content"]}})
                        elif ch.get("text"):
                            self._line({"model": name, "created_at": now_iso(), "done": False, "response": ch["text"]})
                extra = {"tool_calls": tool_calls_from_oai([calls[i] for i in sorted(calls)])} if calls else None
                self._line(self._final(name, t0, usage, reason, key, extra))
            finally:
                conn.close()

        def chat(self, req):
            msgs = messages_to_oai(req.get("messages") or [])
            if not msgs:  # Ollama: chat vacío = cargar el modelo y responder hecho
                return self._json(200, self._final(resolve(req.get("model")), time.time(), None, "load", "message"))
            name, body = self._oai_request(req, messages=msgs)
            return self._run("/v1/chat/completions", name, body, "message")

        def generate(self, req):
            prompt = req.get("prompt") or ""
            if not prompt and not req.get("images"):
                return self._json(200, self._final(resolve(req.get("model")), time.time(), None, "load", "response"))
            if req.get("raw"):
                name, body = self._oai_request(req, prompt=prompt)
                return self._run("/v1/completions", name, body, "response")
            msgs = ([{"role": "system", "content": req["system"]}] if req.get("system") else []) + \
                [{"role": "user", "content": prompt, "images": req.get("images") or []}]
            name, body = self._oai_request(req, messages=messages_to_oai(msgs))
            # se pide como chat (plantilla del modelo) pero se contesta en formato generate
            t0 = time.time()
            if not body["stream"]:
                conn, r = upstream("/v1/chat/completions", body)
                try:
                    raw = r.read()
                    if r.status >= 400:
                        raise ApiError(r.status, raw.decode("utf-8", "replace")[:300])
                    j = json.loads(raw)
                finally:
                    conn.close()
                ch = (j.get("choices") or [{}])[0]
                out = self._final(name, t0, j.get("usage"), ch.get("finish_reason"), "response")
                out["response"] = (ch.get("message") or {}).get("content") or ""
                return self._json(200, out)
            return self._run_chat_as_generate(name, body, t0)

        def _run_chat_as_generate(self, name, body, t0):
            conn, r = upstream("/v1/chat/completions", body)
            try:
                if r.status >= 400:
                    raise ApiError(r.status, r.read().decode("utf-8", "replace")[:300])
                self._start_stream()
                usage, reason = None, None
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                        continue
                    try:
                        j = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    usage = j.get("usage") or usage
                    for ch in j.get("choices") or []:
                        reason = ch.get("finish_reason") or reason
                        t = (ch.get("delta") or {}).get("content")
                        if t:
                            self._line({"model": name, "created_at": now_iso(), "done": False, "response": t})
                self._line(self._final(name, t0, usage, reason, "response"))
            finally:
                conn.close()

        def embed(self, req, legacy):
            name = resolve(req.get("model"))
            inp = req.get("prompt") if legacy else req.get("input")
            if inp is None or inp == "" or inp == []:
                raise ApiError(400, "falta el texto a convertir ('input')")
            conn, r = upstream("/v1/embeddings", {"model": name, "input": inp})
            try:
                raw = r.read()
            finally:
                conn.close()
            if r.status >= 400:
                try:
                    e = json.loads(raw).get("error")
                    msg = e.get("message") if isinstance(e, dict) else e
                except ValueError:
                    msg = None
                raise ApiError(r.status, msg or f"HTTP {r.status}")
            vecs = [d.get("embedding") for d in json.loads(raw).get("data") or []]
            if legacy:
                return self._json(200, {"embedding": vecs[0] if vecs else []})
            return self._json(200, {"model": name, "embeddings": vecs})

        def pull(self, req):
            name = str(req.get("model") or req.get("name") or "").strip()
            if not name:
                raise ApiError(400, "falta el nombre del modelo")
            try:
                job = cl.models.add_ollama(name)
            except Exception as e:  # ModelError y errores de red: mensaje claro al cliente
                raise ApiError(400, str(e))
            jid = job["id"] if isinstance(job, dict) else job
            stream = req.get("stream", True) is not False
            if stream:
                self._start_stream()
                self._line({"status": "pulling manifest"})
            last = None
            while True:
                v = next((x for x in cl.models.views() if x["id"] == jid), None)
                if v is None:
                    break
                if stream and v["status"] == "descargando" and v.get("done") != last:
                    last = v.get("done")
                    self._line({"status": "downloading", "digest": digest(name), "total": v.get("total") or 0,
                                "completed": v.get("done") or 0})
                if v["status"] not in ("en cola", "descargando"):
                    break
                time.sleep(1)
            ok = v is not None and v["status"] == "completada"
            res = {"status": "success"} if ok else {"error": (v or {}).get("error") or "descarga cancelada"}
            if stream:
                return self._line(res)
            return self._json(200 if ok else 500, res)

    return H
