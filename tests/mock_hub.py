# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Servidor simulado de Hugging Face y del registro de Ollama (solo lo que usa dllm/models.py).

- HF: /api/models?search=…, /api/models/<repo>/tree/main?recursive=true, /<repo>/resolve/main/<fichero> → 302 a
  otro host ("CDN", localhost en vez de 127.0.0.1) que sirve el fichero con soporte de Range.
- Ollama: /v2/<ns>/<modelo>/manifests/<tag> y /v2/<ns>/<modelo>/blobs/sha256:<hex>.
Registra qué cabeceras Authorization llegan a cada host para comprobar que el token no se filtra a la CDN.
"""
import hashlib
import io
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


def gguf_bytes(size, arch="llama", n_layers=8, name="mock"):
    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b

    kv = [s("general.architecture") + struct.pack("<I", 8) + s(arch),
          s("general.name") + struct.pack("<I", 8) + s(name),
          s(f"{arch}.block_count") + struct.pack("<II", 4, n_layers),
          s(f"{arch}.context_length") + struct.pack("<II", 4, 8192)]
    head = b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv)) + b"".join(kv)
    body = bytes((i * 7) % 251 for i in range(max(0, size - len(head))))  # no todo ceros: el hash importa
    return head + body


class Hub:
    def __init__(self):
        self.files = {}      # (repo, path) -> bytes
        self.tree_override = {}  # (repo, path) -> dict extra (p. ej. sha incorrecto)
        self.blobs = {}      # "sha256:hex" -> bytes
        self.manifests = {}  # (ns, model, tag) -> dict
        self.auth_seen = {"api": [], "cdn": []}
        self.served = {}     # path -> bytes servidos (para comprobar reanudación)
        self.slow = set()    # ficheros servidos lentamente (para cancelar)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def add_hf(self, repo, path, data, **extra):
        self.files[(repo, path)] = data
        if extra:
            self.tree_override[(repo, path)] = extra

    def add_ollama(self, model, tag, layers, ns="library"):
        """layers: [(mediaType, bytes)]"""
        out = []
        for media, data in layers:
            d = "sha256:" + hashlib.sha256(data).hexdigest()
            self.blobs[d] = data
            out.append({"mediaType": media, "digest": d, "size": len(data)})
        self.manifests[(ns, model, tag)] = {"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                                            "config": {}, "layers": out}

    def close(self):
        self.srv.shutdown()

    def _handler(hub):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _bytes(self, data, key):
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng[6:].split("-")[0])
                chunk = data[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                hub.served[key] = hub.served.get(key, 0) + len(chunk)
                step = 65536
                for i in range(0, len(chunk), step):
                    try:
                        self.wfile.write(chunk[i:i + step])
                    except OSError:
                        return
                    if key in hub.slow:
                        time.sleep(0.05)

            def do_GET(self):
                u = urlparse(self.path)
                p, q = unquote(u.path), parse_qs(u.query)
                host = self.headers.get("Host", "")
                if p.startswith("/cdn/"):
                    hub.auth_seen["cdn"].append(self.headers.get("Authorization"))
                    repo, _, path = p[5:].partition("::")
                    data = hub.files.get((repo, path))
                    return self._bytes(data, path) if data is not None else self._json({"error": "no"}, 404)
                if p.startswith("/api/"):
                    hub.auth_seen["api"].append(self.headers.get("Authorization"))
                if p == "/api/models":
                    term = (q.get("search") or [""])[0].lower()
                    repos = sorted({r for r, _ in hub.files if term in r.lower()})
                    return self._json([{"id": r, "downloads": 100, "likes": 5, "gated": False} for r in repos])
                if p.startswith("/api/models/") and "/tree/main" in p:
                    repo = p[len("/api/models/"):p.index("/tree/main")]
                    entries = []
                    for (r, path), data in sorted(hub.files.items()):
                        if r != repo:
                            continue
                        e = {"type": "file", "path": path, "size": len(data), "oid": "x"}
                        if path.endswith(".gguf"):
                            e["lfs"] = {"oid": hashlib.sha256(data).hexdigest(), "size": len(data)}
                        e.update(hub.tree_override.get((r, path), {}))
                        entries.append(e)
                    return self._json(entries) if entries else self._json({"error": "Repository not found"}, 404)
                if "/resolve/main/" in p:
                    repo, _, path = p[1:].partition("/resolve/main/")
                    if (repo, path) not in hub.files:
                        return self._json({"error": "no"}, 404)
                    # como la CDN de HF: redirección a OTRO host (localhost en vez de 127.0.0.1)
                    other = "localhost" if host.startswith("127.0.0.1") else "127.0.0.1"
                    self.send_response(302)
                    self.send_header("Location", f"http://{other}:{hub.port}/cdn/{repo}::{path}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if p.startswith("/v2/"):
                    parts = p.split("/")  # '', v2, ns, model, kind, ref
                    if len(parts) == 6 and parts[4] == "manifests":
                        m = hub.manifests.get((parts[2], parts[3], parts[5]))
                        return self._json(m) if m else self._json({"errors": [{"code": "MANIFEST_UNKNOWN"}]}, 404)
                    if len(parts) == 6 and parts[4] == "blobs":
                        data = hub.blobs.get(parts[5])
                        return self._bytes(data, parts[5]) if data is not None else self._json({}, 404)
                self._json({"error": "no"}, 404)

        return H
