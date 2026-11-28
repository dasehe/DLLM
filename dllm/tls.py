# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""HTTPS opcional para el panel, la API y la API compatible con Ollama.

- Certificado propio (`--tls-cert`/`--tls-key`, PEM) o autofirmado generado con `openssl` (`--tls-self-signed`, se
  guarda en `~/.dllm/tls/`, clave con permisos 600). Se muestra la huella SHA-256 para comprobarla en el navegador.
- Un solo puerto para los dos protocolos: se mira el primer byte de cada conexión; un *ClientHello* de TLS (0x16) se
  atiende por HTTPS, y el HTTP sin cifrar solo se acepta desde el propio equipo (127.0.0.1: terminal, aplicaciones
  locales y la API Ollama hacia `/v1`). Desde la red, HTTP sin cifrar recibe una redirección a `https://`.
- TLS 1.2 como mínimo."""
import hashlib
import ipaddress
import os
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

from .util import FastHTTPServer

LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class TLSError(ValueError):
    pass


def fingerprint(cert_path):
    """Huella SHA-256 del certificado (formato AA:BB:…, la que muestran los navegadores)."""
    pem = Path(cert_path).read_text(encoding="ascii", errors="replace")
    der = ssl.PEM_cert_to_DER_cert(pem[pem.index("-----BEGIN CERTIFICATE-----"):])
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def make_context(cert, key):
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key))
    except (OSError, ssl.SSLError, ValueError) as e:
        raise TLSError(f"no se pudo cargar el certificado TLS ({cert}, {key}): {e}")
    return ctx


def self_signed(folder, hosts=(), days=825):
    """Crea (si no existe) un certificado autofirmado con `openssl` en `folder` y devuelve (cert, clave)."""
    folder = Path(folder)
    cert, key = folder / "dllm-cert.pem", folder / "dllm-key.pem"
    if cert.is_file() and key.is_file():
        return cert, key
    exe = shutil.which("openssl")
    if not exe:
        raise TLSError("no se encuentra `openssl` para crear el certificado autofirmado: instálalo (en Windows viene con "
                       "Git for Windows) o indica un certificado propio con --tls-cert y --tls-key")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    names = {"localhost", socket.gethostname()}
    ips = {"127.0.0.1"}
    for h in hosts:
        if not h:
            continue
        try:
            ips.add(str(ipaddress.ip_address(h)))
        except ValueError:
            if all(c.isalnum() or c in "-." for c in h):
                names.add(h)
    san = ",".join([f"DNS:{n}" for n in sorted(names) if n] + [f"IP:{i}" for i in sorted(ips)])
    tmp_key = key.with_suffix(".tmp")
    old = os.umask(0o077)
    try:
        r = subprocess.run([exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-keyout", str(tmp_key), "-out", str(cert), "-days", str(days),
                            "-subj", f"/CN=DLLM {socket.gethostname()[:40]}", "-addext", f"subjectAltName={san}"],
                           capture_output=True, text=True, timeout=60)
    finally:
        os.umask(old)
    if r.returncode != 0 or not tmp_key.is_file():
        raise TLSError("openssl no pudo crear el certificado: " + (r.stderr or r.stdout).strip()[-300:])
    os.replace(tmp_key, key)
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    return cert, key


class TLSHTTPServer(FastHTTPServer):
    """Servidor HTTP que también atiende HTTPS en el mismo puerto (ver el docstring del módulo)."""
    ssl_ctx = None
    loopback = LOOPBACK

    def finish_request(self, request, client_address):
        if self.ssl_ctx is None:
            return super().finish_request(request, client_address)
        prev = request.gettimeout()
        request.settimeout(10)
        try:
            first = request.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if not first:
            return
        if first == b"\x16":
            try:
                tls_sock = self.ssl_ctx.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError):
                return  # cliente que no confía en el certificado, escaneos…
            tls_sock.settimeout(prev)
            try:
                return super().finish_request(tls_sock, client_address)
            finally:
                try:
                    tls_sock.close()
                except OSError:
                    pass
        if client_address[0] not in self.loopback:
            return self._redirect(request)
        request.settimeout(prev)
        return super().finish_request(request, client_address)

    def _redirect(self, request):
        """HTTP sin cifrar desde la red: 301 a la misma ruta por https (nada de credenciales en claro)."""
        data = b""
        try:
            while b"\r\n\r\n" not in data and len(data) < 8192:
                chunk = request.recv(2048)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace").split("\r\n")
        parts = head[0].split(" ") if head else []
        path = parts[1] if len(parts) >= 2 and parts[1].startswith("/") else "/"
        host = next((h.split(":", 1)[1].strip() for h in head[1:] if h.lower().startswith("host:")), "")
        if not host or any(c in host for c in " \r\n/\\@"):
            host = f"{self.server_address[0]}:{self.server_address[1]}"
        body = b"This server requires HTTPS.\n"
        loc = f"https://{host}{path}".replace("\r", "").replace("\n", "")
        msg = (f"HTTP/1.1 301 Moved Permanently\r\nLocation: {loc}\r\nContent-Type: text/plain\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("latin-1", "replace") + body
        try:
            request.sendall(msg)
        except OSError:
            pass


def setup(args, home, advertise=None):
    """Lee las opciones TLS de `dllm up` y devuelve (contexto, info) o (None, None)."""
    cert, key = getattr(args, "tls_cert", None), getattr(args, "tls_key", None)
    if getattr(args, "tls_self_signed", False) and not cert:
        cert, key = self_signed(Path(home) / "tls", hosts=[advertise, getattr(args, "host", None)])
        mode = "self-signed"
    elif cert or key:
        if not (cert and key):
            raise TLSError("indica a la vez --tls-cert y --tls-key")
        mode = "files"
    else:
        return None, None
    ctx = make_context(cert, key)
    return ctx, {"mode": mode, "cert": str(cert), "fingerprint": fingerprint(cert)}
