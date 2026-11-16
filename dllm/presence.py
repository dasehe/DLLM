# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Saber si el panel (o el asistente) sigue abierto en algún navegador y esconder/mostrar la consola.

- Cada pestaña del panel avisa cada pocos segundos (`/api/ui/ping?id=…`) y al cerrarse (`/api/ui/bye`, con
  sendBeacon). Sin avisos durante PRESENCE_TIMEOUT se da por cerrada (los navegadores ralentizan los temporizadores de
  las pestañas en segundo plano, por eso el margen es amplio).
- `watch()` lo usan `dllm open` y el asistente: con la consola escondida, si se cierra el navegador y el programa
  sigue en marcha, la consola reaparece con un menú para volver a abrirlo.
"""
import ctypes
import os
import re
import threading
import time

PRESENCE_TIMEOUT = 150.0  # s sin avisos de una pestaña → cerrada
BYE_GRACE = 6.0           # s tras cerrarse la última pestaña antes de preguntar (recargar = bye + ping)
RE_ID = re.compile(r"^[\w-]{4,64}$")


class Presence:
    def __init__(self, timeout=PRESENCE_TIMEOUT):
        self.timeout = timeout
        self.clients = {}
        self.seen = False
        self.lock = threading.Lock()

    def touch(self, cid):
        if cid and RE_ID.match(cid):
            with self.lock:
                self.clients[cid] = time.time()
                self.seen = True

    def bye(self, cid):
        with self.lock:
            self.clients.pop(cid, None)

    def view(self):
        now = time.time()
        with self.lock:
            for k in [k for k, t in self.clients.items() if now - t > self.timeout]:
                del self.clients[k]
            return {"active": len(self.clients), "seen": self.seen}


# ------------------------------------------------------------------ consola (Windows)
def _console():
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.kernel32.GetConsoleWindow() or None
    except Exception:
        return None


def hide_console():
    """Esconde la ventana de consola propia (Windows). Devuelve True si había una que esconder."""
    h = _console()
    if h:
        ctypes.windll.user32.ShowWindow(h, 0)  # SW_HIDE
    return bool(h)


def show_console():
    h = _console()
    if not h:
        return
    u = ctypes.windll.user32
    u.ShowWindow(h, 9)  # SW_RESTORE
    try:  # Windows solo deja pasar al frente tras una pulsación: se simula Alt
        u.keybd_event(0x12, 0, 0, 0)
        u.keybd_event(0x12, 0, 2, 0)
    except Exception:
        pass
    u.SetForegroundWindow(h)


def watch(get_view, reopen, what="el panel", stop=None, poll=2.0, ask=input, out=print, running="DLLM"):
    """Vigila las pestañas abiertas. Cuando se cierra la última (y el programa sigue vivo), muestra la consola y
    pregunta. get_view() → {"active", "seen"} o None si el programa ya no responde. reopen() abre el navegador.
    stop() (opcional) para el programa. Devuelve el motivo de salida: "salir", "parado" o "terminado"."""
    idle_since = None
    while True:
        v = get_view()
        if v is None:
            return "terminado"
        if v["active"]:
            idle_since = None
        elif v["seen"]:
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= BYE_GRACE:
                show_console()
                out(f"\nHas cerrado {what}, pero {running} sigue en marcha.")
                opts = "[A] Abrir de nuevo" + ("   [P] Parar DLLM" if stop else "") + "   [Intro] Salir"
                out(opts + (f"  ({running} seguirá funcionando)" if stop else ""))
                try:
                    c = ask("> ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    c = ""
                if c in ("a", "abrir"):
                    reopen()
                    hide_console()
                    idle_since = None
                    time.sleep(max(poll, BYE_GRACE))  # tiempo para que la pestaña nueva avise
                    continue
                if c in ("p", "parar") and stop:
                    stop()
                    return "parado"
                return "salir"
        time.sleep(poll)
