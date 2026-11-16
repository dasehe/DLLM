# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Utilidades de red: log, pipe bidireccional y túnel autenticado hacia los agentes."""
import socket
import socketserver
import struct
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from . import auth


class FastHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer sin la resolución DNS inversa (socket.getfqdn) que hace HTTPServer al arrancar:
    en macOS y en algunas redes tarda 30 s o más y retrasa el arranque del agente y del orquestador."""
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    def get_request(self):
        # sin TCP_NODELAY, las cabeceras y el cuerpo de cada respuesta salen en dos envíos y el segundo espera al
        # ACK retardado del cliente: ~40 ms extra por petición entre equipos (no en local)
        sock, addr = super().get_request()
        tune(sock)
        return sock, addr


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def tune(sock):
    """TCP_NODELAY: el RPC de ggml intercambia mensajes pequeños y Nagle añadiría latencia."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def pipe(a, b, count=None):
    """Copia bidireccional entre dos sockets hasta que uno cierre. count(dirección, bytes): 'rx' = de a hacia b,
    'tx' = de b hacia a (lo usa el agente para medir la actividad RPC)."""

    def pump(src, dst, direction):
        try:
            while True:
                d = src.recv(262144)
                if not d:
                    break
                dst.sendall(d)
                if count:
                    count(direction, len(d))
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    t = threading.Thread(target=pump, args=(b, a, "tx"), daemon=True)
    t.start()
    pump(a, b, "rx")
    t.join(timeout=5)
    a.close()
    b.close()


def probe_link(host, port, secret, size=4 << 20, timeout=15):
    """Sonda de enlace por el MISMO canal que usa el túnel (puerto del proxy del agente + handshake HMAC mutuo, en
    modo «probe»): RTT mínimo con ecos de 1 byte y ancho de banda efectivo con bloques de `size` bytes en cada
    sentido (el mejor de 2). Lanza excepción si el agente no soporta la sonda (anterior a 0.5.0) o no responde."""
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        tune(s)
        auth.client_handshake(s, secret, probe=True)

        def exch(n_up, n_down):
            hdr = struct.pack(">II", n_up, n_down)
            t = time.perf_counter()
            s.sendall(hdr)
            if n_up:
                s.sendall(bytes(n_up))
            auth.recv_exact(s, 1)
            t_ack = time.perf_counter() - t
            left = n_down
            while left > 0:
                chunk = s.recv(min(left, 1 << 20))
                if not chunk:
                    raise ConnectionError("la sonda se cortó")
                left -= len(chunk)
            return t_ack, time.perf_counter() - t

        exch(1 << 16, 1 << 16)  # calentamiento (ventana TCP)
        rtts = [exch(1, 0)[0] for _ in range(8)]
        up = min(exch(size, 0)[0] for _ in range(2))
        down = min(exch(0, size)[1] for _ in range(2))
        mb = lambda dt: size * 8 / 1e6 / dt
        return {"rtt_ms": round(min(rtts[1:]) * 1000, 2), "up_mbps": round(mb(up)), "down_mbps": round(mb(down)),
                "mbps": round(min(mb(up), mb(down))), "at": time.time(), "via": "tunel"}
    finally:
        s.close()


class Tunnel:
    """Escucha en 127.0.0.1:<puerto efímero> y reenvía cada conexión al proxy autenticado
    de un agente. llama-server se conecta a este puerto local creyendo que habla con
    rpc-server; el túnel añade la autenticación que rpc-server no tiene."""

    def __init__(self, host, port, secret, slot=0):
        self.host, self.remote_port, self.secret, self.slot = host, port, secret, slot
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.failures = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        try:
            r = socket.create_connection((self.host, self.remote_port), timeout=5)
            tune(r)
            tune(c)
            auth.client_handshake(r, self.secret, slot=self.slot)
            r.settimeout(None)
        except Exception as e:
            self.failures += 1
            log(f"túnel → {self.host}:{self.remote_port} falló: {e}")
            c.close()
            return
        pipe(c, r)

    def stop(self):
        try:
            self.srv.close()
        except OSError:
            pass
