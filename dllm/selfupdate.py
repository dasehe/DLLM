# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Actualizar DLLM en los nodos desde el orquestador, con vuelta atrás.

- El orquestador envía su propio `dllm.pyz` al agente (petición firmada con HMAC sobre el sha256 del cuerpo, como el
  resto de la API del agente; además se comprueba el sha256 que viaja en la cabecera). Así todo el clúster queda en la
  misma versión, también sin Internet.
- Antes de sustituir nada se prueba el paquete nuevo (`python dllm.pyz --version` debe decir la versión esperada). Se
  guarda el anterior como `dllm.pyz.bak` y una marca `dllm_update.json`; el agente se reinicia con el nuevo.
- Vuelta atrás automática: si el agente nuevo no aguanta en marcha (se reinicia más de MAX_ATTEMPTS veces sin llegar a
  HEALTHY_S segundos funcionando), se restaura el anterior. También se puede volver atrás a mano desde el panel.
- Se puede desactivar por nodo (`--no-dllm-update` en el agente o el ajuste del nodo en el orquestador)."""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

MAX_ATTEMPTS = 3
HEALTHY_S = 90
MAX_PYZ = 32 << 20
_VER = re.compile(r"^\d+\.\d+\.\d+([.\w+-]{0,20})?$")


class UpdateError(ValueError):
    pass


def current_pyz():
    """Ruta del dllm.pyz con el que se ejecuta este proceso, o None (código fuente)."""
    for cand in (getattr(sys.modules.get("__main__"), "__file__", None), sys.argv[0] if sys.argv else None):
        if not cand:
            continue
        p = Path(cand)
        for q in (p, *p.parents):
            if q.suffix == ".pyz" and q.is_file():
                return q.resolve()
    return None


def marker_path(home):
    return Path(home) / "dllm_update.json"


def read_marker(home):
    try:
        return json.loads(marker_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_marker(home, m):
    p = marker_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m), encoding="utf-8")


def validate(pyz, expected, timeout=60):
    """El paquete nuevo arranca y dice ser la versión esperada."""
    try:
        r = subprocess.run([sys.executable, str(pyz), "--version"], capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, DLLM_LANG="en"))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"el paquete nuevo no arranca: {e}")
    out = (r.stdout + r.stderr).strip()
    if f"dllm {expected}" not in out:
        raise UpdateError("el paquete nuevo no es la versión esperada: " + (out.splitlines() or ["(sin salida)"])[0][:120])


def install(data, sha256, version, home, current_version, pyz=None):
    """Instala `data` (un dllm.pyz) en lugar del actual. Devuelve la ruta. No reinicia (ver restart_soon)."""
    pyz = Path(pyz) if pyz else current_pyz()
    if pyz is None:
        raise UpdateError("este nodo ejecuta DLLM desde el código fuente: actualízalo con git o reinstálalo")
    if not isinstance(version, str) or not _VER.match(version):
        raise UpdateError("versión no válida")
    if not data or len(data) > MAX_PYZ or data[:2] not in (b"#!", b"PK"):
        raise UpdateError("el paquete recibido no parece un dllm.pyz")
    if hashlib.sha256(data).hexdigest() != (sha256 or "").lower():
        raise UpdateError("el sha256 del paquete recibido no coincide: descartado")
    new = pyz.with_name(pyz.name + ".new")
    new.write_bytes(data)
    try:
        os.chmod(new, 0o755)
    except OSError:
        pass
    try:
        validate(new, version)
    except UpdateError:
        new.unlink(missing_ok=True)
        raise
    bak = pyz.with_name(pyz.name + ".bak")
    if pyz.exists():
        os.replace(pyz, bak)
    os.replace(new, pyz)
    _write_marker(home, {"from": current_version, "to": version, "pyz": str(pyz), "bak": str(bak),
                         "t": int(time.time()), "attempts": 0})
    return pyz


def rollback(home, pyz=None):
    """Vuelve al dllm.pyz anterior (dllm.pyz.bak). Devuelve True si había copia."""
    m = read_marker(home) or {}
    pyz = Path(m.get("pyz") or pyz or current_pyz() or "")
    if not pyz or not str(pyz).endswith(".pyz"):
        raise UpdateError("este nodo no ejecuta DLLM desde un dllm.pyz")
    bak = pyz.with_name(pyz.name + ".bak")
    if not bak.is_file():
        raise UpdateError("no hay versión anterior guardada")
    failed = pyz.with_name(pyz.name + ".failed")
    if pyz.exists():
        os.replace(pyz, failed)
    os.replace(bak, pyz)
    try:
        marker_path(home).unlink()
    except OSError:
        pass
    return True


def has_backup(pyz=None):
    pyz = Path(pyz) if pyz else current_pyz()
    return bool(pyz and pyz.with_name(pyz.name + ".bak").is_file())


def startup_guard(home, log=print):
    """Al arrancar: cuenta el intento; si la versión nueva ya falló demasiadas veces, vuelve a la anterior y reinicia.
    Si no, al cabo de HEALTHY_S segundos funcionando se da por buena y se borra la marca."""
    m = read_marker(home)
    if not m:
        return
    m["attempts"] = int(m.get("attempts", 0)) + 1
    if m["attempts"] > MAX_ATTEMPTS:
        log(f"la actualización a DLLM {m.get('to')} no arranca bien: se vuelve a {m.get('from')}")
        try:
            rollback(home)
            restart()
        except (UpdateError, OSError) as e:
            log(f"no se pudo volver a la versión anterior: {e}")
        return
    _write_marker(home, m)

    def healthy():
        time.sleep(HEALTHY_S)
        cur = read_marker(home)
        if cur and cur.get("t") == m.get("t"):
            try:
                marker_path(home).unlink()
            except OSError:
                pass
            log(f"DLLM {m.get('to')} funciona: actualización confirmada (la anterior queda en dllm.pyz.bak)")
    threading.Thread(target=healthy, daemon=True).start()


def restart():
    """Reinicia el proceso con el mismo dllm.pyz y los mismos argumentos (ya sustituido por el nuevo)."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def restart_soon(delay=1.0, log=print):
    def go():
        time.sleep(delay)
        log("reiniciando con la nueva versión de DLLM…")
        restart()
    threading.Thread(target=go, daemon=True).start()
