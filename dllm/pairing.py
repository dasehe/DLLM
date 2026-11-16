# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Emparejamiento de un nodo instalado SIN código de unión: el orquestador lo «reclama» y se confirma en el nodo.

Flujo:
1. El agente sin secreto anuncia por broadcast UDP `DLLMPAIR1 {name, http_port, pid}` (sin datos sensibles).
2. El panel del orquestador lo lista en «Equipos esperando unirse»; al pulsar *Reclamar*, el orquestador manda su
   clave pública Diffie-Hellman a `POST /pair/request`; el agente responde con la suya.
3. Los dos calculan la misma clave y, de ella, un **código de verificación** de 6 cifras que se muestra en el panel y
   en el nodo (asistente o `dllm pair`). La persona comprueba que coinciden y **acepta en el nodo**. Así un tercero en
   la red no puede hacerse pasar por el orquestador (ataque de intermediario): tendría otro código.
4. El orquestador envía el secreto del clúster cifrado y autenticado con esa clave (`POST /pair/complete`). El nodo
   lo guarda y pasa a funcionar como cualquier otro, sin reiniciar.

Criptografía solo con la biblioteca estándar: DH sobre el grupo MODP de 2048 bits (RFC 3526, grupo 14), claves de
256 bits, SHA-256; cifrado con flujo HMAC-SHA256 en modo contador y autenticación HMAC-SHA256 (encrypt-then-MAC).
"""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

PREFIX = b"DLLMPAIR1 "
P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
G = 2
REQUEST_TTL = 300.0  # s para confirmar en el nodo
RE_NAME = re.compile(r"^[\w.\- ]{1,64}$")


# ------------------------------------------------------------------ criptografía
def keypair():
    x = secrets.randbits(256) | (1 << 255)
    return x, pow(G, x, P)


def shared_key(x, peer_pub):
    peer = int(peer_pub)
    if not 2 <= peer <= P - 2:
        raise ValueError("clave pública no válida")
    s = pow(peer, x, P)
    if s in (1, P - 1):
        raise ValueError("clave compartida débil")
    return hashlib.sha256(b"dllm-pair-v1" + s.to_bytes(256, "big")).digest()


def sas(key, pub_orch, pub_node):
    """Código de verificación de 6 cifras ligado a las dos claves públicas («123 456»)."""
    d = hmac.new(key, b"sas" + str(pub_orch).encode() + b"|" + str(pub_node).encode(), hashlib.sha256).digest()
    n = int.from_bytes(d[:8], "big") % 1_000_000
    return f"{n // 1000:03d} {n % 1000:03d}"


def _stream(key, nonce, n):
    out, i = b"", 0
    while len(out) < n:
        out += hmac.new(key, b"enc" + nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return out[:n]


def seal(key, obj):
    data = json.dumps(obj).encode()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(key, nonce, len(data))))
    tag = hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).hexdigest()
    return {"nonce": nonce.hex(), "ct": ct.hex(), "tag": tag}


def open_sealed(key, box):
    try:
        nonce, ct = bytes.fromhex(box["nonce"]), bytes.fromhex(box["ct"])
        tag = hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).hexdigest()
    except (KeyError, TypeError, ValueError):
        raise ValueError("mensaje de emparejamiento mal formado")
    if not hmac.compare_digest(tag, str(box.get("tag", ""))):
        raise ValueError("mensaje de emparejamiento manipulado o con otra clave")
    return json.loads(bytes(a ^ b for a, b in zip(ct, _stream(key, nonce, len(ct)))))


# ------------------------------------------------------------------ anuncios
def beacon(name, http_port, pid):
    return PREFIX + json.dumps({"name": name, "http_port": http_port, "pid": pid}).encode()


def parse_beacon(data):
    if not data.startswith(PREFIX):
        return None
    try:
        d = json.loads(data[len(PREFIX):])
        port = int(d["http_port"])
        name, pid = str(d["name"])[:64], str(d["pid"])[:32]
    except (ValueError, KeyError, TypeError):
        return None
    if not (1 <= port <= 65535 and RE_NAME.match(name) and re.match(r"^\w{4,32}$", pid)):
        return None
    return {"name": name, "http_port": port, "pid": pid}


# ------------------------------------------------------------------ lado del nodo
class NodePairing:
    """Estado del emparejamiento en el agente. Una sola solicitud a la vez; se confirma localmente."""

    def __init__(self, on_paired):
        self.pid = secrets.token_hex(8)
        self.on_paired = on_paired  # fn(datos descifrados) → guarda la configuración y activa el nodo
        self.req = None
        self.lock = threading.Lock()
        self.hits = []

    def _rate_ok(self):
        now = time.time()
        self.hits = [t for t in self.hits if now - t < 60]
        self.hits.append(now)
        return len(self.hits) <= 10

    def request(self, ip, body):
        """POST /pair/request desde el orquestador: {orch, pub}. Devuelve {id, pub}."""
        with self.lock:
            if not self._rate_ok():
                raise PermissionError("demasiadas solicitudes; espera un minuto")
            cur = self.req
            if cur and cur["state"] in ("pendiente", "aceptada") and time.time() - cur["t"] < REQUEST_TTL \
                    and cur["ip"] != ip:
                raise PermissionError("ya hay otra solicitud de emparejamiento en curso")
            orch = str(body.get("orch") or "orquestador")[:64]
            x, pub = keypair()
            key = shared_key(x, body.get("pub"))
            self.req = {"id": secrets.token_hex(8), "ip": ip, "orch": orch, "key": key, "t": time.time(),
                        "sas": sas(key, body.get("pub"), pub), "state": "pendiente"}
            return {"id": self.req["id"], "pub": str(pub)}

    def _current(self, rid=None):
        r = self.req
        if not r or (rid is not None and r["id"] != rid):
            return None
        if r["state"] in ("pendiente", "aceptada") and time.time() - r["t"] > REQUEST_TTL:
            r["state"] = "caducada"
        return r

    def status(self, rid):
        r = self._current(rid)
        return {"state": r["state"] if r else "desconocida"}

    def local_view(self):
        """Para el asistente o `dllm pair` en el propio nodo (solo desde 127.0.0.1)."""
        r = self._current()
        if not r:
            return {"state": "esperando", "pid": self.pid}
        return {"state": r["state"], "id": r["id"], "orch": r["orch"], "ip": r["ip"], "sas": r["sas"],
                "expires_in": max(0, int(REQUEST_TTL - (time.time() - r["t"])))}

    def decide(self, rid, accept):
        with self.lock:
            r = self._current(rid)
            if not r or r["state"] != "pendiente":
                raise ValueError("no hay ninguna solicitud pendiente con ese identificador")
            r["state"] = "aceptada" if accept else "rechazada"
            return {"state": r["state"]}

    def complete(self, ip, body):
        """POST /pair/complete: {id, box} con el secreto cifrado. Solo tras aceptar en el nodo."""
        with self.lock:
            r = self._current(body.get("id"))
            if not r or r["ip"] != ip:
                raise PermissionError("solicitud desconocida")
            if r["state"] != "aceptada":
                raise PermissionError("el emparejamiento no se ha aceptado en el nodo")
            data = open_sealed(r["key"], body.get("box") or {})
            if not isinstance(data.get("secret"), str) or len(data["secret"]) < 16:
                raise ValueError("secreto no válido")
            r["state"] = "completada"
        self.on_paired(dict(data, orch_ip=data.get("orch_ip") or ip))
        return {"ok": True}
