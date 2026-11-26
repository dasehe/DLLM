# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Terminar los procesos de prueba en cualquier sistema (Windows no tiene os.killpg ni SIGKILL)."""
import os
import signal
import subprocess

SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def killpg(pid, sig=signal.SIGTERM):
    """Mata el grupo de procesos (Unix) o el árbol de procesos (Windows, taskkill /T)."""
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/T"] + (["/F"] if sig == SIGKILL else []) + ["/PID", str(pid)],
                           capture_output=True)
        if r.returncode not in (0, 128) and b"not found" in r.stderr.lower():
            raise ProcessLookupError(pid)
        return
    os.killpg(pid, sig)
