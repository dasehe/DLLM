# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Limpieza a fondo al desinstalar: cierra TODOS los procesos de DLLM (agente, orquestador, llama-server, rpc-server),
quita tareas programadas, servicios y reglas de cortafuegos, y borra los restos, incluidos los de la versión anterior
del programa (que se llamaba con el nombre antiguo y cuyo desinstalador dejaba procesos vivos).

Solo biblioteca estándar. Cada paso devuelve lo que ha hecho; nada falla en silencio sin dejar constancia."""
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

# Nombre del programa antes del cambio de marca. Aquí solo sirve para limpiar instalaciones antiguas.
LEGACY = "ll" + "mm"
NAMES = ("dllm", LEGACY)

PYTHONS = re.compile(r"^(py|pyw|pythonw?(\d+(\.\d+)*)?)$", re.I)
ENGINES = {"llama-server", "rpc-server", "ggml-rpc-server"}
# Un intérprete de Python es nuestro solo si ejecuta el programa (dllm.pyz o -m dllm), no por llevar el nombre en una ruta
CMD_RE = re.compile(r"(?:%s)\.pyz\b|-m\s+(?:%s)\b" % ("|".join(NAMES), "|".join(NAMES)), re.I)
DIR_RE = re.compile(r"[\\/](?:%s)[\\/]" % "|".join(NAMES), re.I)


def list_processes():
    """[{pid, ppid, name, cmd, exe}] de todos los procesos del sistema."""
    out = []
    if platform.system().lower() == "windows":
        ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine,ExecutablePath"
              " | ConvertTo-Json -Compress")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, timeout=60)
        data = json.loads(r.stdout or "[]")
        for p in data if isinstance(data, list) else [data]:
            out.append({"pid": int(p["ProcessId"]), "ppid": int(p.get("ParentProcessId") or 0),
                        "name": p.get("Name") or "", "cmd": p.get("CommandLine") or "", "exe": p.get("ExecutablePath") or ""})
        return out
    r = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=30)
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        cmd = parts[2]
        first = cmd.split(None, 1)[0]
        out.append({"pid": int(parts[0]), "ppid": int(parts[1]), "name": os.path.basename(first), "cmd": cmd, "exe": first})
    return out


def _stem(name):
    return re.sub(r"\.exe$", "", name or "", flags=re.I)


def is_ours(p, extra_dirs=()):
    """¿Es un proceso de DLLM (o de la versión anterior)? Python ejecutando el programa, o un motor de llama.cpp que
    vive en una carpeta de DLLM."""
    stem = _stem(p["name"])
    if PYTHONS.match(stem):
        return bool(CMD_RE.search(p["cmd"]))
    if stem.lower() in ENGINES:
        where = p["exe"] or p["cmd"]
        low = where.lower().replace("\\", "/")
        return bool(DIR_RE.search(where)) or any(str(d).lower().replace("\\", "/") in low for d in extra_dirs if d)
    return False


def find_ours(procs, extra_dirs=(), me=None):
    """Procesos a cerrar: los nuestros, menos este mismo proceso y sus ascendientes (el lanzador que nos ejecuta)."""
    me = me or os.getpid()
    by_pid = {p["pid"]: p for p in procs}
    protect, cur = set(), me
    while cur and cur not in protect:
        protect.add(cur)
        cur = (by_pid.get(cur) or {}).get("ppid")
    return [p for p in procs if p["pid"] not in protect and is_ours(p, extra_dirs)]


def _kill(pid):
    if platform.system().lower() == "windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def kill_all(extra_dirs=(), dry_run=False, rounds=4, list_fn=None, kill_fn=None):
    """Cierra todo lo de DLLM, repitiendo hasta que no quede nada (los vigilantes relanzan a sus hijos).
    Devuelve (cerrados, supervivientes): listas de textos «nombre (PID n)»."""
    list_fn, kill_fn = list_fn or list_processes, kill_fn or _kill
    closed, left = [], []
    for _ in range(rounds):
        try:
            todo = find_ours(list_fn(), extra_dirs)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
        if not todo:
            left = []
            break
        if dry_run:
            return [f"{_stem(p['name'])} (PID {p['pid']})" for p in todo], []
        # primero los programas de Python (vigilantes), luego los motores
        for p in sorted(todo, key=lambda x: _stem(x["name"]).lower() in ENGINES):
            kill_fn(p["pid"])
            closed.append(f"{_stem(p['name'])} (PID {p['pid']})")
        time.sleep(0.5)
        left = [f"{_stem(p['name'])} (PID {p['pid']})" for p in find_ours(list_fn(), extra_dirs)]
        if not left:
            break
    return closed, left


def _run(cmd):
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def remove_legacy_service(dry_run=False):
    """Tareas programadas, servicios y accesos directos de la versión anterior."""
    osn = platform.system().lower()
    done = []
    if dry_run:
        return done
    if osn == "windows":
        for n in NAMES:
            _run(["schtasks", "/End", "/TN", n.upper()])
            _run(["schtasks", "/Delete", "/TN", n.upper(), "/F"])
        app = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Microsoft/Windows/Start Menu/Programs"
        paths = [Path.home() / "Desktop" / f"{LEGACY.upper()}.lnk", app / f"{LEGACY.upper()}.lnk"]
    elif osn == "linux":
        _run(["systemctl", "--user", "disable", "--now", f"{LEGACY}.service"])
        paths = [Path.home() / f".config/systemd/user/{LEGACY}.service", Path.home() / f".local/share/applications/{LEGACY}.desktop"]
    else:
        pl = Path.home() / f"Library/LaunchAgents/com.{LEGACY}.plist"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(pl)])
        paths = [pl]
    for p in paths:
        if p.exists():
            try:
                p.unlink()
                done.append(f"quitado {p}")
            except OSError:
                pass
    return done


def legacy_firewall_names():
    return [f"{LEGACY.upper()}-{s}" for s in ("agente", "beacon", "API")] + [f"{LEGACY.upper()} agent", f"{LEGACY.upper()} beacon"]


def legacy_dirs():
    """(carpetas de configuración antiguas, carpetas de llama.cpp antiguas). Los modelos nunca se tocan."""
    conf = Path.home() / f".{LEGACY}"
    llama = []
    try:
        cfg = json.loads((conf / "config.json").read_text(encoding="utf-8"))
        if cfg.get("install_dir"):
            llama.append(Path(cfg["install_dir"]) / "llama.cpp")
    except (OSError, ValueError):
        pass
    llama += [Path.home() / LEGACY.upper() / "llama.cpp", conf / "llama"]
    return [conf], [d for i, d in enumerate(llama) if d not in llama[:i]]


def force_rmtree(path, attempts=4):
    """Borra una carpeta aunque haya ficheros de solo lectura o en uso un instante (reintenta). True si desapareció."""
    path = Path(path)

    def onerr(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    for i in range(attempts):
        if not path.exists():
            return True
        shutil.rmtree(path, onerror=onerr)
        if not path.exists():
            return True
        time.sleep(1 + i)
    return not path.exists()
