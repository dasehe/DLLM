# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Copia de seguridad y restauración de TODA la configuración del orquestador (un solo fichero).

Contenido (carpeta de configuración, ~/.dllm): config.json (rol, ajustes, secreto del clúster, API key), cluster.json,
models.json (parámetros de cada modelo), secrets.json (token de Hugging Face), nodes.json (ajustes por nodo),
nodes_added.json (nodos añadidos por IP) y, si se pide, events.jsonl (registros). Además un inventario de los modelos
instalados (los .gguf pesan decenas de GB y NO van dentro: el inventario permite volver a descargarlos).

Con el secreto del clúster restaurado, los nodos existentes vuelven a conectarse solos: no hay que reinstalarlos.

Formato: ZIP (manifest.json con sha256 de cada fichero + files/<nombre>). La copia contiene secretos, así que se cifra
con una contraseña, solo con la biblioteca estándar:
  clave = PBKDF2-HMAC-SHA256 (600 000 iteraciones, sal aleatoria) → 64 bytes (cifrado | autenticación);
  cifrado = HMAC-SHA256 como PRF en modo contador (flujo de clave), «cifrar y luego autenticar» con HMAC-SHA256;
  la etiqueta se verifica (comparación en tiempo constante) ANTES de descifrar nada.
Para archivarla a largo plazo también puedes cifrar el fichero resultante con age o GPG."""
import hashlib
import hmac
import io
import json
import os
import secrets
import struct
import time
import zipfile
from pathlib import Path

FORMAT = 1
MAGIC_ENC = b"DLLMBK1E"      # cifrado
MAGIC_ZIP = b"PK\x03\x04"    # sin cifrar (zip)
ITERATIONS = 600_000
MAX_BACKUP = 16 << 20         # tamaño máximo del fichero que se acepta
MAX_MEMBER = 12 << 20         # y de cada fichero descomprimido (anti zip-bomb)
CORE = ("config.json", "cluster.json", "models.json", "secrets.json", "nodes.json", "nodes_added.json", "users.json",
        "webhooks.json")
OPTIONAL = ("events.jsonl",)
ALLOWED = CORE + OPTIONAL
EXT = ".dllmbak"


class BackupError(ValueError):
    pass


# ------------------------------------------------------------------ cifrado
def _keys(passphrase, salt, iters):
    k = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iters, 64)
    return k[:32], k[32:]


def _stream(enc_key, nonce, n):
    out, ctr = bytearray(), 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + struct.pack(">Q", ctr), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def encrypt(data, passphrase):
    if len(passphrase or "") < 8:
        raise BackupError("la contraseña debe tener al menos 8 caracteres")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    ek, mk = _keys(passphrase, salt, ITERATIONS)
    ct = bytes(a ^ b for a, b in zip(data, _stream(ek, nonce, len(data))))
    head = MAGIC_ENC + struct.pack(">I", ITERATIONS) + salt + nonce
    return head + ct + hmac.new(mk, head + ct, hashlib.sha256).digest()


def is_encrypted(blob):
    return blob[:8] == MAGIC_ENC


def decrypt(blob, passphrase):
    if len(blob) < 8 + 4 + 32 + 32 or blob[:8] != MAGIC_ENC:
        raise BackupError("no es una copia de DLLM cifrada")
    iters = struct.unpack(">I", blob[8:12])[0]
    if not 100_000 <= iters <= 5_000_000:
        raise BackupError("parámetros de cifrado no válidos")
    salt, nonce, ct, tag = blob[12:28], blob[28:44], blob[44:-32], blob[-32:]
    ek, mk = _keys(passphrase or "", salt, iters)
    if not hmac.compare_digest(hmac.new(mk, blob[:44] + ct, hashlib.sha256).digest(), tag):
        raise BackupError("contraseña incorrecta o fichero dañado")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct))))


# ------------------------------------------------------------------ crear / leer
def build(home, inventory=None, version="", host="", include_logs=False, passphrase=None):
    """Crea la copia. `inventory`: lista de modelos instalados. Sin contraseña queda sin cifrar (hay que pedirlo
    explícitamente: contiene el secreto del clúster)."""
    home = Path(home)
    files, names = {}, list(CORE) + (list(OPTIONAL) if include_logs else [])
    for n in names:
        p = home / n
        if p.is_file() and p.stat().st_size <= MAX_MEMBER:
            files[n] = p.read_bytes()
    if "config.json" not in files and "cluster.json" not in files:
        raise BackupError("no hay configuración que copiar (¿está instalado DLLM en este equipo?)")
    manifest = {"format": FORMAT, "app": "dllm", "version": version, "created": int(time.time()), "host": host,
                "encrypted": bool(passphrase), "contains_secrets": True,
                "files": {n: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for n, b in files.items()},
                "models": list(inventory or [])}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for n, b in files.items():
            z.writestr("files/" + n, b)
    raw = buf.getvalue()
    return encrypt(raw, passphrase) if passphrase else raw


def open_backup(blob, passphrase=None):
    """Valida y abre una copia. Devuelve (manifest, {nombre: bytes}). Lanza BackupError si algo no cuadra."""
    if not blob or len(blob) > MAX_BACKUP:
        raise BackupError("fichero vacío o demasiado grande para ser una copia de DLLM")
    if is_encrypted(blob):
        if not passphrase:
            raise BackupError("la copia está cifrada: indica la contraseña")
        blob = decrypt(blob, passphrase)
    if blob[:4] != MAGIC_ZIP:
        raise BackupError("no es una copia de DLLM")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        man = json.loads(z.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise BackupError("la copia está dañada")
    if man.get("app") != "dllm" or man.get("format") != FORMAT:
        raise BackupError(f"formato de copia no soportado ({man.get('app')} v{man.get('format')})")
    files, total = {}, 0
    for name, meta in (man.get("files") or {}).items():
        if name not in ALLOWED:           # lista cerrada: nada de rutas arbitrarias (zip-slip)
            raise BackupError(f"la copia contiene un fichero no permitido: {name}")
        try:
            info = z.getinfo("files/" + name)
        except KeyError:
            raise BackupError(f"falta {name} en la copia")
        if info.file_size > MAX_MEMBER:
            raise BackupError(f"{name} es demasiado grande")
        total += info.file_size
        if total > 2 * MAX_MEMBER:
            raise BackupError("la copia descomprimida es demasiado grande")
        b = z.read(info)
        if hashlib.sha256(b).hexdigest() != meta.get("sha256"):
            raise BackupError(f"{name}: la suma de comprobación no coincide (copia modificada o dañada)")
        if name.endswith(".json"):
            try:
                json.loads(b)
            except ValueError:
                raise BackupError(f"{name}: no es JSON válido")
        files[name] = b
    return man, files


def _j(files, name, default=None):
    try:
        return json.loads(files[name])
    except (KeyError, ValueError):
        return default


def summary(man, files, home=None):
    """Resumen legible de lo que contiene (y qué cambiaría) sin aplicar nada."""
    cfg = _j(files, "config.json", {}) or {}
    cur = {}
    if home:
        try:
            cur = json.loads((Path(home) / "config.json").read_text())
        except (OSError, ValueError):
            cur = {}
    sec = cfg.get("secret") or (_j(files, "cluster.json", {}) or {}).get("secret")
    cur_sec = cur.get("secret")
    models = man.get("models") or []
    return {
        "version": man.get("version"), "created": man.get("created"), "host": man.get("host"),
        "encrypted": man.get("encrypted"), "role": cfg.get("role"), "name": cfg.get("name"),
        "files": {n: m["size"] for n, m in (man.get("files") or {}).items()},
        "has_secret": bool(sec), "secret_changes": bool(cur_sec and sec and cur_sec != sec),
        "api_key": bool((cfg.get("orchestrator") or {}).get("api_key")),
        "hf_token": bool((_j(files, "secrets.json", {}) or {}).get("hf_token")),
        "model_settings": len(_j(files, "models.json", {}) or {}),
        "node_settings": len(_j(files, "nodes.json", {}) or {}),
        "nodes_added": len(_j(files, "nodes_added.json", []) or []),
        "has_logs": "events.jsonl" in files,
        "models": models,
    }


# ------------------------------------------------------------------ restaurar
def _write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def merge_config(imported, local):
    """config.json restaurado: del clúster se toma lo que define su identidad (secreto, API key y ajustes del
    orquestador y del agente); lo propio de este equipo (rol, nombre, rutas, versión de llama.cpp) se queda como está,
    porque en un equipo nuevo las carpetas serán otras."""
    out = dict(local or {})
    for k in ("secret", "orchestrator", "agent"):
        if imported.get(k) is not None:
            out[k] = imported[k]
    if not local:          # no había configuración local: se toma entera
        out = dict(imported)
    return out


def restore(files, home, safety=True):
    """Aplica la copia en `home`. Antes guarda una copia de seguridad de lo que había (home/backups/…). Devuelve las
    acciones realizadas y la ruta de la copia previa."""
    home = Path(home)
    actions, safety_path = [], None
    if safety:
        try:
            safety_path = str(make_safety(home))
        except BackupError:
            safety_path = None
    for name, data in files.items():
        if name == "config.json":
            try:
                local = json.loads((home / name).read_text())
            except (OSError, ValueError):
                local = None
            cfg = merge_config(json.loads(data), local)
            _write(home / name, json.dumps(cfg, indent=2, ensure_ascii=False).encode())
            actions.append("config.json: secreto, API key y ajustes del clúster")
        elif name == "events.jsonl":
            _write(home / name, data)
            actions.append("registros históricos")
        else:
            _write(home / name, data)
            actions.append(name)
    sec = None
    try:
        sec = json.loads(files.get("config.json") or b"{}").get("secret")
    except ValueError:
        pass
    if sec:  # que auth.load_secret() lo encuentre también si falta config.json
        _write(home / "cluster.json", json.dumps({"secret": sec}).encode())
    return actions, safety_path


def make_safety(home):
    """Copia del estado actual (sin cifrar, permisos 600) antes de pisarlo."""
    home = Path(home)
    blob = build(home, version="pre-restauración", host="", include_logs=False)
    out = home / "backups"
    out.mkdir(parents=True, exist_ok=True)
    p = out / (time.strftime("antes-de-restaurar-%Y%m%d-%H%M%S") + ".zip")
    _write(p, blob)
    for old in sorted(out.glob("antes-de-restaurar-*.zip"))[:-5]:   # conserva las 5 últimas
        try:
            old.unlink()
        except OSError:
            pass
    return p


def filename(host=""):
    h = "".join(c for c in (host or "dllm") if c.isalnum() or c in "-_")[:30] or "dllm"
    return f"dllm-{h}-{time.strftime('%Y%m%d-%H%M')}{EXT}"
