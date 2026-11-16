# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Motores de inferencia: lo que un slot de modelo necesita saber de su proceso, sin asumir `llama-server`.

`EngineDriver` es la interfaz; `LlamaCppDriver` es la única implementación hoy. Añadir un motor nuevo (imagen, audio…)
es registrar otro driver en `DRIVERS` con sus endpoints y su comando: el pool de slots, el libro de memoria y el
enrutador L7 no cambian.

- `capabilities()`: qué sabe hacer (`chat`, `embeddings`, y a futuro `image`, `video`, `stt`, `tts`).
- `supported_endpoints()`: rutas HTTP → capacidad que exigen.
- `build_command(spec)`: línea de comandos del proceso (siempre escuchando solo en 127.0.0.1).
- `healthcheck(port)`: ¿está listo?
- `estimate_memory(model, ctx, settings)`: {"vram", "ram"} que necesita según su `device_policy`.
- `default_placement()`: `split` (repartido entre nodos por RPC) o `node` (fijado a un único nodo, p. ej. embeddings).
"""
import http.client

from . import models, scheduler

# ruta OpenAI → capacidad. Las rutas multimodales están reservadas para drivers futuros.
OPENAI_ROUTES = {
    "/v1/chat/completions": "chat", "/v1/completions": "chat", "/v1/embeddings": "embeddings",
    "/v1/images/generations": "image", "/v1/videos/generations": "video",
    "/v1/audio/transcriptions": "stt", "/v1/audio/speech": "tts",
}


def capability_of(path):
    """Capacidad que exige una ruta de la API (None si no es una ruta de inferencia conocida)."""
    return OPENAI_ROUTES.get(path)


class EngineDriver:
    name = "base"
    binary = None  # nombre del ejecutable (para el gestor de binarios por motor)

    def __init__(self, settings=None):
        self.settings = settings or {}

    def capabilities(self):
        raise NotImplementedError

    def supported_endpoints(self):
        caps = self.capabilities()
        return {p: c for p, c in OPENAI_ROUTES.items() if c in caps}

    def build_command(self, spec):
        raise NotImplementedError

    def healthcheck(self, port, timeout=2.0):
        raise NotImplementedError

    def estimate_memory(self, model, ctx, settings=None):
        raise NotImplementedError

    def default_placement(self):
        return "split"


class LlamaCppDriver(EngineDriver):
    name = "llamacpp"
    binary = "llama-server"

    @property
    def embeddings(self):
        return bool(self.settings.get("embeddings"))

    def capabilities(self):
        return {"embeddings"} if self.embeddings else {"chat"}

    def default_placement(self):
        return "node" if self.embeddings else "split"

    def build_command(self, spec):
        """spec: bin, model_path, port, ctx, ngl (def. 999), rpc [host:puerto], tensor_split [float], device_args [str],
        mmproj (ruta o None), extra [str]."""
        cmd = [spec["bin"], "-m", spec["model_path"], "--host", "127.0.0.1", "--port", str(spec["port"]),
               "-c", str(spec["ctx"]), "-ngl", str(spec.get("ngl", 999))]
        if spec.get("rpc"):
            cmd += ["--rpc", ",".join(spec["rpc"])]
        if spec.get("tensor_split"):
            cmd += ["-ts", ",".join(f"{s:.4f}" for s in spec["tensor_split"])]
        cmd += list(spec.get("device_args") or [])
        cmd += ["--metrics"] + models.load_args(self.settings)
        if self.embeddings:
            cmd += ["--embeddings"]
        if spec.get("mmproj"):
            # visión/audio: el proyector se ejecuta en el orquestador (CPU), no se reparte entre los nodos
            cmd += ["--mmproj", spec["mmproj"], "--no-mmproj-offload"]
        cmd += list(spec.get("extra") or [])
        return cmd

    def healthcheck(self, port, timeout=2.0):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            try:
                c.request("GET", "/health")
                return c.getresponse().status == 200
            finally:
                c.close()
        except OSError:
            return False

    def estimate_memory(self, model, ctx, settings=None):
        """cpu_only: todo en RAM del orquestador. En el resto: todo en VRAM/pool de los nodos y buffers de E/S en RAM."""
        s = settings or self.settings
        total = scheduler.need_bytes(model, ctx, s.get("cache_type_k") or "f16", s.get("cache_type_v") or "f16")
        if s.get("device_policy") == "cpu_only":
            return {"vram": 0, "ram": total}
        return {"vram": total, "ram": scheduler.IO_RAM}


DRIVERS = {"llamacpp": LlamaCppDriver}


def driver_for(settings=None):
    """Driver de un modelo según sus ajustes (`engine`; por defecto llama.cpp)."""
    return DRIVERS[(settings or {}).get("engine", "llamacpp")](settings)
