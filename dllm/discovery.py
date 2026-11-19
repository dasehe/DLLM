# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Búsqueda y alta manual de nodos por IP, para cuando el anuncio por broadcast no llega (VLAN, Wi-Fi con aislamiento,
VPN, cortafuegos). Solo biblioteca estándar.

Un agente DLLM responde sin autenticación únicamente en `/pair/*` y solo mientras no está emparejado:
  · `GET /pair/status?id=x` → 200 {"state": "desconocida"}      → agente libre (se puede reclamar)
  · 404 «ya está emparejado»                                    → agente de otro clúster (o del nuestro)
  · 503 «nodo sin emparejar» en el resto de rutas               → idem, libre
Con eso se distingue «libre», «ya en este clúster» y «de otro clúster» sin enviar ningún secreto."""
import http.client
import ipaddress
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PORT = 8765
MAX_HOSTS = 1024          # /22 como máximo: la búsqueda es solo para redes locales
RE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")


def valid_host(host):
    """IP o nombre de host válidos (sin esquema, ruta ni puerto)."""
    host = (host or "").strip()
    if not host or len(host) > 253:
        raise ValueError("indica la IP o el nombre del equipo")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not RE_HOST.match(host):
        raise ValueError("IP o nombre de equipo no válido")
    return host


def valid_port(port, default=DEFAULT_PORT):
    try:
        p = int(port) if port not in (None, "") else default
    except (TypeError, ValueError):
        raise ValueError("puerto no válido")
    if not 1 <= p <= 65535:
        raise ValueError("el puerto debe estar entre 1 y 65535")
    return p


def hosts_of(cidr):
    """Direcciones a explorar de un rango (CIDR o «a.b.c.d-e»). Solo redes privadas, máximo MAX_HOSTS."""
    cidr = (cidr or "").strip()
    try:
        m = re.match(r"^(\d+\.\d+\.\d+\.)(\d+)-(?:\1)?(\d+)$", cidr)
        if m:
            lo, hi = int(m.group(2)), int(m.group(3))
            if not 0 <= lo <= hi <= 255:
                raise ValueError
            ips = [ipaddress.ip_address(f"{m.group(1)}{i}") for i in range(lo, hi + 1)]
        else:
            net = ipaddress.ip_network(cidr, strict=False)
            if net.version != 4:
                raise ValueError("solo IPv4")
            ips = list(net.hosts()) if net.num_addresses <= MAX_HOSTS * 2 else []
    except ValueError as e:
        raise ValueError(f"rango no válido ({e or 'usa 192.168.1.0/24 o 192.168.1.10-50'})")
    if not ips or len(ips) > MAX_HOSTS:
        raise ValueError(f"el rango debe tener entre 1 y {MAX_HOSTS} direcciones")
    if not all(i.is_private or i.is_loopback for i in ips):
        raise ValueError("solo se buscan redes privadas (10.x, 172.16-31.x, 192.168.x)")
    return [str(i) for i in ips]


def default_range(lan_ip):
    """El /24 de la IP local del orquestador."""
    try:
        return str(ipaddress.ip_network(f"{lan_ip}/24", strict=False))
    except ValueError:
        return "192.168.1.0/24"


def probe(host, port, timeout=1.0):
    """Clasifica lo que hay en host:port. Devuelve {"state": "libre"|"emparejado"|"otro"|None}.
    None = nada que responda como un agente DLLM."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    s.close()
    c = http.client.HTTPConnection(host, port, timeout=max(timeout, 1.5))
    try:
        c.request("GET", "/pair/status?id=sondeo")
        r = c.getresponse()
        data = r.read(4096)
        if r.status == 200:
            try:
                if "state" in json.loads(data or b"{}"):
                    return {"state": "libre"}
            except ValueError:
                pass
            return None
        if r.status == 404 and b"emparejado" in data:
            return {"state": "emparejado"}
        return None
    except (OSError, http.client.HTTPException):
        return None
    finally:
        c.close()


def scan(hosts, port, progress, workers=64, timeout=0.6):
    """Explora `hosts` en paralelo; `progress(done, found_item_or_None)` se llama por cada uno."""
    def one(h):
        try:
            r = probe(h, port, timeout)
        except Exception:
            r = None
        progress(1, dict(r, host=h, port=port) if r else None)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, hosts))
