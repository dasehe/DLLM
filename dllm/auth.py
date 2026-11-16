# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Autenticación del clúster: secreto compartido + HMAC.

- API HTTP de los agentes: cada petición va firmada (hora, método, ruta, hash del cuerpo).
- Túnel RPC: handshake de reto-respuesta mutuo antes de dejar pasar un solo byte hacia rpc-server.
- Beacons de descubrimiento UDP: firmados para que nadie pueda registrar nodos falsos.

Ojo: esto AUTENTICA pero no CIFRA. Los tensores viajan en claro por la LAN.
Para cifrar, poner los nodos en una VLAN dedicada o sobre WireGuard.
"""
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from pathlib import Path

EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def conf_dir():
    """Carpeta de configuración (se evalúa en cada llamada para respetar DLLM_HOME)."""
    return Path(os.environ.get("DLLM_HOME") or (Path.home() / ".dllm"))


def new_secret():
    return secrets.token_urlsafe(32)


def load_secret(cli=None):
    if cli:
        return cli
    env = os.environ.get("DLLM_SECRET")
    if env:
        return env
    cfg = conf_dir() / "config.json"
    if cfg.exists():
        try:
            s = json.loads(cfg.read_text()).get("secret")
            if s:
                return s
        except (OSError, ValueError):
            pass
    f = conf_dir() / "cluster.json"
    if f.exists():
        return json.loads(f.read_text())["secret"]
    return None


def save_secret(secret):
    conf_dir().mkdir(parents=True, exist_ok=True)
    f = conf_dir() / "cluster.json"
    f.write_text(json.dumps({"secret": secret}))
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return f


# ---------------------------------------------------------------- HTTP firmado
def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sign_request(secret, method, path, bhash, ts=None):
    ts = str(int(ts if ts is not None else time.time()))
    msg = f"{ts}\n{method}\n{path}\n{bhash}".encode()
    return ts, hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_request(secret, method, path, bhash, ts, sig, skew=60):
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - t) > skew:
        return False
    _, exp = sign_request(secret, method, path, bhash, t)
    return hmac.compare_digest(exp, sig or "")


# ---------------------------------------------------------------- handshake TCP
def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("conexión cerrada durante el handshake")
        buf += chunk
    return buf


def _mac(secret, tag, a, b):
    return hmac.new(secret.encode(), tag + a + b, hashlib.sha256).digest()


MAX_SLOTS = 8  # modelos simultáneos por clúster (el slot 0 usa la etiqueta de siempre: compatible con 0.4.x)


def server_handshake(sock, secret):
    """Lado agente: reta al cliente y se autentica ante él. Devuelve "rpc" (túnel normal) o "probe" (sonda de
    enlace: el cliente firmó con la etiqueta b"p"; un agente anterior a 0.5.0 la rechaza como MAC inválido)."""
    ns = os.urandom(16)
    sock.sendall(ns)
    data = recv_exact(sock, 48)
    nc, mac = data[:16], data[16:]
    if hmac.compare_digest(mac, _mac(secret, b"c", ns, nc)):
        mode = "rpc"
    elif hmac.compare_digest(mac, _mac(secret, b"p", ns, nc)):
        mode = "probe"
    else:
        mode = None
        for slot in range(1, MAX_SLOTS):  # slots de modelo adicionales: la etiqueta lleva el número de slot
            if hmac.compare_digest(mac, _mac(secret, b"c" + bytes([slot]), ns, nc)):
                mode = f"rpc:{slot}"
                break
        if mode is None:
            raise PermissionError("MAC de cliente inválido")
    sock.sendall(_mac(secret, b"s", ns, nc))
    return mode


def client_handshake(sock, secret, probe=False, slot=0):
    """Lado orquestador: responde al reto y verifica que el agente conoce el secreto."""
    ns = recv_exact(sock, 16)
    nc = os.urandom(16)
    sock.sendall(nc + _mac(secret, b"p" if probe else (b"c" if not slot else b"c" + bytes([slot])), ns, nc))
    resp = recv_exact(sock, 32)
    if not hmac.compare_digest(resp, _mac(secret, b"s", ns, nc)):
        raise PermissionError("el agente no se autenticó")


# ---------------------------------------------------------------- sonda de enlace (tras el handshake)
PROBE_MAX = 8 << 20  # bytes máximos por sentido y petición
PROBE_REQS = 32      # peticiones máximas por conexión


def serve_probe(sock):
    """Lado agente, ya autenticado en modo "probe": repite peticiones (n_subida, n_bajada). Lee n_subida bytes
    del cliente y responde con 1 byte de acuse + n_bajada bytes. Tamaños acotados; no toca rpc-server."""
    for _ in range(PROBE_REQS):
        try:
            hdr = recv_exact(sock, 8)
        except ConnectionError:
            return
        n_up, n_down = struct.unpack(">II", hdr)
        if n_up > PROBE_MAX or n_down > PROBE_MAX:
            raise ValueError("sonda: tamaño no permitido")
        left = n_up
        while left > 0:
            chunk = sock.recv(min(left, 1 << 20))
            if not chunk:
                raise ConnectionError("sonda: conexión cerrada")
            left -= len(chunk)
        sock.sendall(b"\x01")
        sent, block = 0, bytes(1 << 16)
        while sent < n_down:
            k = min(len(block), n_down - sent)
            sock.sendall(block[:k])
            sent += k


# ---------------------------------------------------------------- beacons UDP
def sign_beacon(secret, payload: dict) -> bytes:
    p = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(secret.encode(), p.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"p": p, "sig": sig}).encode()


def verify_beacon(secret, data: bytes, skew=120):
    try:
        env = json.loads(data)
        p, sig = env["p"], env["sig"]
        exp = hmac.new(secret.encode(), p.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(exp, sig):
            return None
        d = json.loads(p)
        if abs(time.time() - float(d["ts"])) > skew:
            return None
        return d
    except Exception:
        return None
