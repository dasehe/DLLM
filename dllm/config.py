# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Configuración persistente del equipo (config.json) y código de unión (join code)."""
import base64
import json
import os
import socket
import zlib
from pathlib import Path

from . import auth

PREFIX = "DLLM1-"


def home():
    return auth.conf_dir()


def path():
    return home() / "config.json"


def default_install_dir():
    """Carpeta por defecto para llama.cpp y los modelos (lo pesado). La configuración y el secreto se quedan en
    home() (~/.dllm), con permisos restringidos."""
    return str(Path.home() / "DLLM")


def llama_dir(cfg=None):
    """Dónde se descarga llama.cpp: <carpeta de DLLM>/llama.cpp, o ~/.dllm/llama en instalaciones antiguas."""
    d = (cfg or {}).get("install_dir")
    return Path(d) / "llama.cpp" if d else home() / "llama"


def default(role="node"):
    return {
        "version": 1, "role": role, "name": socket.gethostname(), "secret": None,
        "bin_dir": None, "llama_tag": None, "install_dir": None, "models_dir": str(Path.home() / "models"),
        "orchestrator": {"host": "127.0.0.1", "port": 8080, "api_key": None, "min_mbps": 800,
                         "beacon_port": 50999},
        "agent": {"port": 8765, "rpc_port": 50051, "backend": "auto", "reserve_gb": 1.0,
                  # contribución: 'auto' = la gestiona el orquestador; 'manual' = fijada en este equipo
                  "limits_mode": "auto", "share": None, "mem_gb": None, "threads": None,
                  "bw_gbps": None, "announce_to": [], "allow_ip": [], "cache": False,
                  "device": None, "auto_update": True},
    }


def load():
    try:
        return json.loads(path().read_text())
    except (OSError, ValueError):
        return None


def save(cfg):
    home().mkdir(parents=True, exist_ok=True)
    p = path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(tmp, 0o600)  # contiene el secreto del clúster
    except OSError:
        pass
    os.replace(tmp, p)
    return p


def redacted(cfg):
    """Copia sin secretos, para mostrar en UI."""
    if not cfg:
        return None
    c = json.loads(json.dumps(cfg))
    c["secret"] = "***" if c.get("secret") else None
    if c.get("orchestrator", {}).get("api_key"):
        c["orchestrator"]["api_key"] = "***"
    return c


def lan_ip():
    """IP local por la que se sale a la red (no envía tráfico: UDP connect solo elige ruta)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- argv por rol
def agent_argv(cfg):
    a = cfg["agent"]
    argv = ["agent"] + (["--secret", cfg["secret"]] if cfg.get("secret") else ["--pairing"])
    if a.get("restrict_ip") is False:
        argv += ["--no-restrict-pair"]
    argv += ["--name", cfg["name"], "--port", str(a["port"]),
            "--rpc-port", str(a["rpc_port"]), "--bin-dir", cfg["bin_dir"], "--backend", a["backend"],
            "--reserve-gb", str(a["reserve_gb"]), "--beacon-port", str(cfg["orchestrator"]["beacon_port"])]
    argv += ["--limits-mode", a.get("limits_mode") or "auto", "--config-file", str(path())]
    if cfg.get("llama_backend"):
        argv += ["--llama-backend", cfg["llama_backend"]]
    if cfg.get("install_dir"):
        argv += ["--install-dir", cfg["install_dir"]]
    if a.get("device"):
        argv += ["--device", a["device"]]
    if a.get("auto_update") is False:
        argv += ["--no-auto-update"]
    if a.get("dllm_update") is False:
        argv += ["--no-dllm-update"]
    if a.get("share") is not None:
        argv += ["--share", str(a["share"])]
    if a.get("threads") is not None:
        argv += ["--threads", str(a["threads"])]
    if a.get("mem_gb") is not None:
        argv += ["--mem-gb", str(a["mem_gb"])]
    if a.get("bw_gbps") is not None:
        argv += ["--bw-gbps", str(a["bw_gbps"])]
    if a.get("cache"):
        argv += ["--cache"]
    for t in a.get("announce_to") or []:
        argv += ["--announce-to", t]
    for ip in a.get("allow_ip") or []:
        argv += ["--allow-ip", ip]
    return argv


def up_argv(cfg):
    o = cfg["orchestrator"]
    argv = ["up", "--secret", cfg["secret"], "--models-dir", cfg["models_dir"], "--host", o["host"],
            "--port", str(o["port"]), "--bin-dir", cfg["bin_dir"], "--min-mbps", str(o["min_mbps"]),
            *(["--on-demand"] if o.get("on_demand") else []),
            "--beacon-port", str(o["beacon_port"])]
    if o.get("api_key"):
        argv += ["--api-key", o["api_key"]]
    if o.get("auto_sync") is False:
        argv += ["--no-auto-sync"]
    if o.get("ollama_port") is not None:  # API compatible con Ollama (0 = desactivada)
        argv += ["--ollama-port", str(o["ollama_port"])]
    if cfg.get("advertise_host"):
        argv += ["--advertise-host", cfg["advertise_host"]]
    t = o.get("tls") or {}
    if t.get("cert") and t.get("key"):  # HTTPS con certificado propio
        argv += ["--tls-cert", t["cert"], "--tls-key", t["key"]]
    elif t.get("mode") == "self-signed":
        argv += ["--tls-self-signed"]
    if cfg["role"] == "both":  # el agente local es un nodo más
        argv += ["--node", f"127.0.0.1:{cfg['agent']['port']}"]
    return argv


# ---------------------------------------------------------------- código de unión
def _crc(raw):
    return format(zlib.crc32(raw.encode()) & 0xFFFFFFFF, "08x")


def encode_join(d):
    """Código corto que el nodo pega en su asistente: secreto + dirección del orquestador + versión de llama.cpp.
    Es información sensible (lleva el secreto): tratarlo como una contraseña."""
    d = dict(d, v=1)
    raw = base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{PREFIX}{raw}-{_crc(raw)}"


def decode_join(code):
    code = "".join((code or "").split())  # tolera espacios y saltos de línea al copiar
    if not code.startswith(PREFIX):
        raise ValueError(f"el código de unión debe empezar por {PREFIX}")
    raw, _, crc = code[len(PREFIX):].rpartition("-")
    if not raw or _crc(raw) != crc.lower():
        raise ValueError("código incompleto o con errores: cópialo de nuevo entero")
    try:
        d = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        raise ValueError("código ilegible")
    for k in ("secret", "orch", "beacon"):
        if not d.get(k):
            raise ValueError(f"al código le falta '{k}'")
    return d
