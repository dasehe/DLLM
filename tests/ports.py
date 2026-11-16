# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Puertos libres sin repetir dentro de una misma ejecución de tests (evita carreras entre llamadas sucesivas)."""
import socket

_used = set()


def free_port():
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if p not in _used:
            _used.add(p)
            return p
