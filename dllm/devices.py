# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Detección de dispositivos de cómputo preguntando al propio llama.cpp (vale para NVIDIA, AMD, Intel y Apple).

- En el nodo: `rpc-server -d <nombre inexistente>` lista los dispositivos que ve, con su memoria, y termina.
  Por defecto rpc-server expone TODOS los aceleradores del equipo; el agente elige uno y lo fija con `-d`.
  (El backend Vulkan ya descarta las gráficas integradas si hay una dedicada.)
- En el orquestador: `llama-server --list-devices` con y sin `--rpc …`. La diferencia son los dispositivos remotos,
  en orden. Se usan solo esos (`--device`), así la GPU local del orquestador no entra en el reparto. Esto no depende
  de cómo nombre llama.cpp a los dispositivos RPC.

Formato de cada línea (llama.cpp):  `Vulkan0: AMD Radeon RX 7600 XT (16368 MiB, 15800 MiB free)`
"""
import re
import subprocess

MiB = 1 << 20
# nombre sin espacios (puede contener ':' y corchetes, p. ej. RPC[127.0.0.1:50052]) + ':' + descripción + memoria
LINE = re.compile(r"^\s*(\S+?):\s+(.+?)\s+\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)\s*$")
PROBE_NAME = "__dllm_probe__"


def parse(text):
    out = []
    for line in (text or "").splitlines():
        m = LINE.match(line)
        if m:
            out.append({"name": m.group(1), "desc": m.group(2).strip(),
                        "total": int(m.group(3)) * MiB, "free": int(m.group(4)) * MiB})
    return out


def is_cpu(d):
    return d["name"].upper().startswith("CPU")


def kind(d):
    """Familia del dispositivo a partir de su nombre (CUDA0 → CUDA, Vulkan1 → Vulkan…)."""
    m = re.match(r"[A-Za-z]+", d["name"])
    return m.group(0) if m else d["name"]


def _run(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + "\n" + r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"no se pudieron listar los dispositivos con {cmd[0]}: {e}")


def probe_rpc_server(rpc_bin, timeout=60):
    """Dispositivos que puede exponer este rpc-server (incluye CPU solo si no hay aceleradores)."""
    return parse(_run([rpc_bin, "-d", PROBE_NAME], timeout))


def probe_llama_server(llama_bin, rpc=None, timeout=120):
    cmd = [llama_bin] + (["--rpc", ",".join(rpc)] if rpc else []) + ["--list-devices"]
    return parse(_run(cmd, timeout))


def choose(devs, preferred=None):
    """Acelerador a usar: el indicado (por nombre o parte de la descripción) o el de más memoria. None = solo CPU."""
    accel = [d for d in devs if not is_cpu(d)]
    if preferred:
        p = preferred.lower()
        for d in devs:
            if d["name"].lower() == p:
                return None if is_cpu(d) else d
        for d in accel:
            if p in d["desc"].lower():
                return d
        avail = ", ".join(f"{d['name']} ({d['desc']})" for d in devs) or "ninguno"
        raise ValueError(f"dispositivo '{preferred}' no encontrado; disponibles: {avail}")
    return max(accel, key=lambda d: (d["total"], d["free"])) if accel else None


def remote(local, with_rpc):
    """Dispositivos que aparecen al añadir --rpc: los remotos, en el orden en que los enumera llama.cpp."""
    names = {d["name"] for d in local}
    return [d for d in with_rpc if d["name"] not in names]
