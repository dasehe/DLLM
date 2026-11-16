# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Instalación de llama.cpp (binarios oficiales), copia del programa, servicios de arranque y diagnóstico.

Notas de diseño:
- Los binarios se descargan de las releases oficiales de ggml-org/llama.cpp y se verifican con el
  sha256 que publica la API de GitHub (si lo da). Se extraen con comprobación anti path-traversal.
- Todos los equipos deben usar la MISMA build: el orquestador publica la etiqueta (bNNNN) en el
  código de unión y los nodos descargan exactamente esa.
- Windows: se descarga el zip 'win-cpu' y encima el del backend (vulkan/cuda/rocm/sycl) en la misma carpeta.
- CUDA (Windows y Linux): se elige el paquete de la versión de CUDA más alta que admita el driver
  (nvidia-smi) y se añade su runtime (cudart). Si no se puede usar, el asistente prueba Vulkan y luego CPU.
"""
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from . import config, hw

GH_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
UA = {"User-Agent": "dllm-installer"}
EXE = ".exe" if os.name == "nt" else ""


class InstallError(Exception):
    pass


# ============================================================ plataforma
def _cuda_version():
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=8)
        m = re.search(r"CUDA Version:\s*([\d.]+)", r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def _other_gpus(osn):
    try:
        if osn == "linux" and shutil.which("lspci"):
            out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=8).stdout
            return [l.split(": ", 1)[-1] for l in out.splitlines()
                    if re.search(r"VGA|3D|Display", l) and re.search(r"AMD|ATI|Intel", l)]
        if osn == "windows":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_VideoController).Name"],
                                 capture_output=True, text=True, timeout=15).stdout
            return [l.strip() for l in out.splitlines() if re.search(r"AMD|Radeon|Intel", l)]
    except Exception:
        pass
    return []


def platform_info():
    osn = {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(platform.system().lower(),
                                                                         platform.system().lower())
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64" if m in ("x86_64", "amd64") else m
    total, avail = hw.mem_info()
    return {"os": osn, "arch": arch, "python": platform.python_version(), "cores": os.cpu_count() or 1,
            "ram_total": total, "ram_free": avail, "nvidia": hw.nvidia_gpus(), "cuda": _cuda_version(),
            "other_gpu": _other_gpus(osn), "host": platform.node()}


# Backends con paquete oficial por sistema y arquitectura, del más rápido al más compatible
BACKENDS_BY_OS = {
    ("windows", "x64"): ["cuda", "vulkan", "rocm", "sycl", "cpu"],
    ("windows", "arm64"): ["cuda", "vulkan", "cpu"],
    ("linux", "x64"): ["cuda", "vulkan", "rocm", "sycl", "cpu"],
    ("linux", "arm64"): ["cuda", "vulkan", "cpu"],
    ("macos", "arm64"): ["metal"],
    ("macos", "x64"): ["cpu"],
}
MIN_CUDA = (12, 4)  # el paquete CUDA más antiguo que publica llama.cpp
BACKEND_NOTES = {
    "cuda": "NVIDIA, el más rápido (necesita driver con CUDA ≥ 12.4)",
    "vulkan": "cualquier GPU (AMD, Intel, NVIDIA) con driver actualizado",
    "rocm": "AMD con ROCm/HIP instalado (experimental; Vulkan suele ir igual de bien)",
    "sycl": "Intel Arc con oneAPI instalado (experimental)",
    "metal": "Apple Silicon (memoria unificada)",
    "cpu": "sin GPU",
}


def suggest_backends(info):
    """(sugerido, [alternativas válidas en esta plataforma, de la más rápida a la más compatible]).
    NVIDIA con driver suficiente → CUDA; otra GPU (o NVIDIA antigua) → Vulkan; sin GPU → CPU; Mac ARM → Metal."""
    opts = BACKENDS_BY_OS.get((info["os"], info.get("arch", "x64")), ["cpu"])
    if info["os"] == "macos":
        return opts[0], opts
    cuda_ok = bool(info["nvidia"]) and _ver(info["cuda"]) >= MIN_CUDA
    gpu = bool(info["nvidia"] or info["other_gpu"])
    sug = "cuda" if cuda_ok and "cuda" in opts else ("vulkan" if gpu and "vulkan" in opts else "cpu")
    return sug, opts


def fallback_backends(backend, info):
    """Orden de reintento si el backend elegido no arranca: el elegido, luego Vulkan (si hay GPU) y CPU."""
    _, opts = suggest_backends(info)
    out = [backend]
    for b in ("vulkan", "cpu"):
        if b in opts and b not in out and (b == "cpu" or info["nvidia"] or info["other_gpu"]):
            out.append(b)
    return out


def _ver(s):
    try:
        return tuple(int(x) for x in str(s).split("."))
    except ValueError:
        return (0,)


# ============================================================ releases
# Paquetes que debe tener una versión para considerarla «completa»: así cualquier nodo que se una al clúster
# (Windows CPU/Vulkan/CUDA, Linux CPU/Vulkan, Mac) encuentra el suyo de la MISMA versión que el orquestador.
CORE_ASSETS = (r"-bin-win-cpu-x64\.zip$", r"-bin-win-vulkan-x64\.zip$", r"-bin-win-cuda-[\d.]+-x64\.zip$",
               r"-bin-ubuntu-x64\.tar\.gz$", r"-bin-ubuntu-vulkan-x64\.tar\.gz$",
               r"-bin-ubuntu-cuda-[\d.]+-x64\.tar\.gz$", r"-bin-macos-arm64\.tar\.gz$")
RE_BUILD_TAG = re.compile(r"b(\d+)")


def _gh_json(url):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        raise InstallError(f"no se pudo consultar las releases de llama.cpp ({e}). "
                           "Sin Internet: usa 'carpeta existente' o DLLM_RELEASE_FILE.")


def _release(data):
    assets = [{"name": a["name"], "url": a["browser_download_url"], "size": a.get("size", 0),
               "sha256": (a.get("digest") or "").replace("sha256:", "") or None} for a in data.get("assets", [])]
    return {"tag": data["tag_name"], "assets": assets}


def missing_core(rel):
    names = [a["name"] for a in rel["assets"]]
    return [p for p in CORE_ASSETS if not any(re.search(p, n) for n in names)]


def choose_release(releases):
    """La compilación bNNNN más reciente con todos los paquetes habituales. Se ignoran borradores y etiquetas que no
    son compilaciones (p. ej. «v1.0.0», que llama.cpp marca como Latest pero no trae binarios). Las compilaciones
    bNNNN se aceptan aunque estén marcadas como pre-release: desde v1.0.0, llama.cpp las publica así."""
    builds = [r for r in releases if RE_BUILD_TAG.fullmatch(r.get("tag_name", "")) and not r.get("draft")]
    builds.sort(key=lambda r: int(r["tag_name"][1:]), reverse=True)
    for r in builds:
        rel = _release(r)
        if not missing_core(rel):
            return rel
    if builds:
        raise InstallError("ninguna versión reciente de llama.cpp publica todos los paquetes (Windows, Linux, Mac); "
                           "vuelve a intentarlo más tarde o usa una carpeta con llama.cpp ya instalado")
    raise InstallError("no se encontraron compilaciones de llama.cpp (etiquetas bNNNN) en sus releases")


def fetch_release(tag=None):
    """Devuelve {'tag', 'assets': [{name,url,size,sha256}]}. Sin etiqueta: la compilación completa más reciente
    (ver choose_release). DLLM_RELEASE_FILE (JSON de una release o lista de releases) permite instalar sin Internet."""
    f = os.environ.get("DLLM_RELEASE_FILE")
    if f:
        data = json.loads(Path(f).read_text())
        if isinstance(data, list):
            if tag:
                hit = next((r for r in data if r.get("tag_name") == tag), None)
                if not hit:
                    raise InstallError(f"la versión {tag} de llama.cpp no existe")
                return _release(hit)
            return choose_release(data)
        return _release(data)
    if tag:
        if not RE_BUILD_TAG.fullmatch(tag):
            raise InstallError(f"'{tag}' no es una compilación de llama.cpp (se esperaba bNNNN)")
        return _release(_gh_json(f"{GH_API}/tags/{tag}"))
    return choose_release(_gh_json(f"{GH_API}?per_page=40"))


def _find(assets, pattern):
    rx = re.compile(pattern)
    return next((a for a in assets if rx.search(a["name"])), None)


def _pick_cuda(assets, osp, arch, cuda):
    """(paquete, runtime) de la versión de CUDA más alta que admite el driver. osp: 'win' o 'ubuntu'."""
    ext = r"\.zip" if osp == "win" else r"\.tar\.gz"
    cands = []
    for a in assets:
        m = re.match(rf"^llama-.*-bin-{osp}-cuda-(?:cu)?([\d.]+)-{arch}{ext}$", a["name"])
        if m and _ver(m.group(1)) <= (_ver(cuda) if cuda else (99,)):
            cands.append((_ver(m.group(1)), m.group(1), a))
    if not cands:
        raise InstallError(f"ningún paquete CUDA de llama.cpp es compatible con tu driver (CUDA {cuda or '?'}): "
                           "actualiza el driver de NVIDIA o usa Vulkan")
    _, ver, pkg = max(cands, key=lambda c: c[0])
    rt = _find(assets, rf"^cudart-.*{osp}-cuda-(?:cu)?{re.escape(ver)}-{arch}{ext}$")
    return pkg, rt


def pick_assets(assets, osn, arch, backend, cuda=None):
    """Lista de archivos a descargar: el primero trae los ejecutables, el resto se superponen."""
    ext = r"\.(?:tar\.gz|zip)$"
    extra = []
    if osn == "macos":
        main = _find(assets, rf"^llama-.*-bin-macos-{arch}{ext}")
    elif osn == "linux":
        if backend == "cuda":
            main, rt = _pick_cuda(assets, "ubuntu", arch, cuda)
            extra += [rt] if rt else []
        else:
            mid = {"cpu": "", "vulkan": "vulkan-", "rocm": r"rocm-[\d.]+-", "sycl": "sycl-fp16-"}.get(backend)
            if mid is None:
                raise InstallError(f"no hay binarios precompilados de '{backend}' para Linux "
                                   "(usa cuda/vulkan/rocm/sycl/cpu o compila con scripts/build-llamacpp.sh)")
            main = _find(assets, rf"^llama-.*-bin-ubuntu-{mid}{arch}{ext}")
    elif osn == "windows":
        main = _find(assets, rf"^llama-.*-bin-win-(?:cpu|avx2)-{arch}\.zip$")
        if backend == "cuda":
            pkg, rt = _pick_cuda(assets, "win", arch, cuda)
            extra += [pkg] + ([rt] if rt else [])
        elif backend in ("vulkan", "rocm", "sycl"):
            pat = {"vulkan": "vulkan", "rocm": r"(?:rocm|hip)[\w.-]*", "sycl": "sycl"}[backend]
            ov = _find(assets, rf"^llama-.*-bin-win-{pat}-{arch}\.zip$")
            if not ov:
                name = {"vulkan": "Vulkan", "rocm": "ROCm", "sycl": "SYCL"}[backend]
                raise InstallError(f"esta versión de llama.cpp no incluye el paquete {name} para Windows")
            extra.append(ov)
    else:
        raise InstallError(f"sistema no soportado: {osn}")
    if not main:
        names = ", ".join(a["name"] for a in assets[:12])
        raise InstallError(f"no hay paquete de llama.cpp para {osn}/{arch}/{backend} en esta release. "
                           f"Disponibles: {names} …")
    return [main] + extra


def download(url, dest, sha256=None, progress=None):
    """Descarga con barra de progreso y verificación sha256. Devuelve True si se verificó el hash."""
    h, got = hashlib.sha256(), 0
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress and total:
                    progress(int(got * 100 / total))
    except Exception as e:
        raise InstallError(f"descarga fallida ({url}): {e}")
    if sha256:
        if h.hexdigest() != sha256.lower():
            Path(dest).unlink(missing_ok=True)
            raise InstallError("el sha256 de la descarga no coincide con el publicado: archivo descartado")
        return True
    return False


def safe_extract(archive, dest):
    """Extrae zip/tar.gz rechazando rutas fuera de `dest` y enlaces que escapen."""
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    def inside(p):
        p = Path(os.path.realpath(p))
        return p == dest or dest in p.parents

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                if not inside(dest / m.filename):
                    raise InstallError(f"ruta peligrosa en el archivo: {m.filename}")
            z.extractall(dest)
            for m in z.infolist():
                mode = m.external_attr >> 16
                if mode & 0o111 and not m.is_dir():
                    os.chmod(dest / m.filename, 0o755)
        return
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            if not inside(dest / m.name):
                raise InstallError(f"ruta peligrosa en el archivo: {m.name}")
            if m.issym() and not inside((dest / m.name).parent / m.linkname):
                raise InstallError(f"enlace simbólico peligroso: {m.name} -> {m.linkname}")
            if m.islnk() and not inside(dest / m.linkname):
                raise InstallError(f"enlace duro peligroso: {m.name} -> {m.linkname}")
            if m.isdev() or m.isfifo():
                raise InstallError(f"entrada especial no permitida: {m.name}")
        try:
            t.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12
            t.extractall(dest)


def locate_bin(root, need=("llama-server", "rpc-server")):
    """Carpeta (dentro de root) que contiene los ejecutables necesarios."""
    for d, _, files in os.walk(root):
        if all(any(a + EXE in files for a in hw.bin_names(n)) for n in need):
            return Path(d)
    return None


def bin_path(bin_dir, name):
    """Ruta del ejecutable `name` en bin_dir, aceptando sus nombres alternativos (ggml-rpc-server/rpc-server)."""
    paths = [Path(bin_dir or "") / (a + EXE) for a in hw.bin_names(name)]
    return next((p for p in paths if p.is_file()), paths[0])


# Códigos NTSTATUS con los que Windows impide cargar un ejecutable o una DLL
WIN_BLOCKED = {0xC0E90002: "sac", 0xC0000428: "firma", 0xC000007B: "imagen", 0xC0000135: "dll"}
SAC_HINT = ("Windows lo ha bloqueado: el «Control inteligente de aplicaciones» (Smart App Control) no deja cargar las "
            "DLL sin firmar de llama.cpp (error 0xc0e90002). Desactívalo en Seguridad de Windows → Control de "
            "aplicaciones y navegador → Configuración de Control inteligente de aplicaciones → Desactivado, y vuelve a "
            "instalar. Ver docs/SOLUCION-PROBLEMAS.md")


def explain_exit(code):
    """Texto para un código de salida de Windows que indica un bloqueo de carga, o None."""
    kind = WIN_BLOCKED.get((code or 0) & 0xFFFFFFFF)
    if kind == "sac":
        return SAC_HINT
    if kind == "firma":
        return "Windows exige una firma que el ejecutable no tiene (0xc0000428): revisa la directiva de control de aplicaciones"
    if kind == "imagen":
        return "ejecutable o DLL de otra arquitectura o dañado (0xc000007b): reinstala llama.cpp"
    if kind == "dll":
        return "falta una DLL (0xc0000135): reinstala llama.cpp o el runtime de tu GPU"
    return None


def run_check(binary):
    """(ok, texto) de ejecutar el binario: detecta librerías que faltan, bloqueos de Windows, etc.
    llama-server responde a --version; rpc-server no lo admite, así que se le pide --help (carga igualmente
    todos los backends, que es lo que puede fallar, imprime el uso y sale con 0)."""
    rpc = hw.is_rpc_server(binary)
    try:
        r = subprocess.run([str(binary), "--help" if rpc else "--version"], capture_output=True, text=True,
                           timeout=60)
    except subprocess.TimeoutExpired:
        msg = "no respondió a --version en 60 s"
        if hw.smart_app_control() in ("on", "eval"):
            msg += ". " + SAC_HINT
        return False, msg
    except Exception as e:
        return False, str(e)
    out = (r.stdout + r.stderr).strip()
    if r.returncode and explain_exit(r.returncode):
        return False, explain_exit(r.returncode)
    if rpc:
        return r.returncode == 0 and "usage:" in out.lower(), out
    return bool(re.search(r"version:\s*\d+", out)), out


def tag_from_build(build):
    m = re.match(r"(\d+)", build or "")
    return f"b{m.group(1)}" if m else None


def verify_bins(bin_dir, need):
    """Comprueba que los ejecutables existen y arrancan; devuelve la build de llama-server o rpc-server."""
    bid = None
    for n in need:
        p = bin_path(bin_dir, n)
        if not p.is_file():
            raise InstallError(f"falta {' o '.join(a + EXE for a in hw.bin_names(n))} en {bin_dir}")
        ok, out = run_check(p)
        if not ok:
            hint = ""
            if "libvulkan" in out:
                hint = " (instala el driver Vulkan: sudo apt install libvulkan1)"
            elif "libgomp" in out:
                hint = " (instala libgomp1)"
            raise InstallError(f"{n} no arranca: {out[:600]}{hint}")
        bid = bid or hw.build_id(str(p))
    return bid


def install_llama(dest_root, tag, backend, info, progress, need=("llama-server", "rpc-server")):
    """Descarga e instala llama.cpp. progress(pct:int, msg:str). Devuelve {bin_dir, tag, build, verified}."""
    pinned = bool(tag)
    rel = fetch_release(tag)
    tag = rel["tag"]
    target = Path(dest_root) / f"llama-{tag}-{backend}"
    bin_dir = locate_bin(target, need) if target.exists() else None
    verified = None
    if bin_dir:
        progress(100, f"llama.cpp {tag} ya instalado")
    else:
        try:
            assets = pick_assets(rel["assets"], info["os"], info["arch"], backend, info.get("cuda"))
        except InstallError as e:
            if not pinned:
                raise
            raise InstallError(f"{e}. La versión {tag} la fija el orquestador (todos los equipos deben usar la "
                               "misma) y no publicó ese paquete: actualiza DLLM en el orquestador y vuelve a "
                               "ejecutar su asistente para que elija una versión completa; después usa su nuevo "
                               "código de unión en este equipo")
        with tempfile.TemporaryDirectory() as tmp:
            for i, a in enumerate(assets):
                p = Path(tmp) / a["name"]
                verified = download(a["url"], p, a["sha256"],
                                    lambda pct, i=i, a=a: progress(pct, f"descargando {a['name']} ({i + 1}/{len(assets)})"))
                progress(100, f"extrayendo {a['name']}")
                safe_extract(p, target)
        bin_dir = locate_bin(target, need)
        if not bin_dir:
            raise InstallError(f"el paquete descargado no contiene {' y '.join(need)} (ni ggml-rpc-server). "
                               "Ese binario precompilado no incluye RPC: compila desde fuente con "
                               "scripts/build-llamacpp.sh")
    if os.name != "nt":
        for f in bin_dir.iterdir():
            if f.is_file() and not f.suffix:
                f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    if info["os"] == "macos":  # quita la cuarentena de Gatekeeper de lo descargado
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(target)], capture_output=True)
    build = verify_bins(bin_dir, need)
    return {"bin_dir": str(bin_dir), "tag": tag, "build": build, "backend": backend, "verified": verified}


# ============================================================ programa y servicio
def running_from_pyz():
    a = Path(sys.argv[0])
    return a.is_file() and zipfile.is_zipfile(a)


def install_program():
    """Copia el .pyz a ~/.dllm para que el servicio no dependa de la carpeta de descarga."""
    if not running_from_pyz():
        return None
    dst = config.home() / "dllm.pyz"
    config.home().mkdir(parents=True, exist_ok=True)
    src = Path(sys.argv[0]).resolve()
    if not (dst.exists() and src == dst.resolve()):
        shutil.copy2(src, dst)
    return dst


def entry_cmd(pyz=None):
    py = sys.executable
    if os.name == "nt":  # pythonw: sin ventana de consola
        pw = Path(py).with_name("pythonw.exe")
        py = str(pw) if pw.exists() else py
    return ([py, str(pyz), "run"] if pyz else [py, "-m", "dllm", "run"]), \
        ({} if pyz else {"PYTHONPATH": str(Path(__file__).resolve().parent.parent)})


def systemd_unit(cmd, env=None):
    envs = "".join(f"Environment={shlex.quote(f'{k}={v}')}\n" for k, v in (env or {}).items())
    return ("[Unit]\nDescription=DLLM (Distributed LLM)\nAfter=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nExecStart={' '.join(shlex.quote(c) for c in cmd)}\nRestart=always\nRestartSec=3\n"
            f"{envs}NoNewPrivileges=true\nPrivateTmp=true\n\n[Install]\nWantedBy=default.target\n")


def launchd_plist(cmd, env=None):
    log = str(config.home() / "dllm.log")
    d = {"Label": "com.dllm", "ProgramArguments": cmd, "RunAtLoad": True, "KeepAlive": True,
         "StandardOutPath": log, "StandardErrorPath": log}
    if env:
        d["EnvironmentVariables"] = env
    return plistlib.dumps(d)


def schtasks_create(cmd):
    return ["schtasks", "/Create", "/TN", "DLLM", "/SC", "ONLOGON", "/TR", subprocess.list2cmdline(cmd), "/F"]


def _run(cmd, dry):
    if dry:
        return True, "(simulado)"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def install_service(cmd, env=None, dry_run=False):
    """Instala y arranca el servicio de usuario (sin permisos de administrador)."""
    osn = platform.system().lower()
    notes, manual = [], " ".join(shlex.quote(c) for c in cmd)
    if osn == "linux":
        if not shutil.which("systemctl"):
            return {"kind": "none", "ok": False, "path": None, "note": "systemd no disponible", "manual": manual}
        unit = Path.home() / ".config/systemd/user/dllm.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(cmd, env))
        ok1, o1 = _run(["systemctl", "--user", "daemon-reload"], dry_run)
        ok2, o2 = _run(["systemctl", "--user", "enable", "--now", "dllm.service"], dry_run)
        if not (ok1 and ok2):
            return {"kind": "systemd", "ok": False, "path": str(unit), "manual": manual,
                    "note": f"no se pudo activar el servicio de usuario ({(o2 or o1)[:160]})"}
        user = os.environ.get("USER") or ""
        okl, _ = _run(["loginctl", "enable-linger", user], dry_run) if user else (False, "")
        if not okl:
            notes.append("para que arranque sin iniciar sesión: sudo loginctl enable-linger " + user)
        return {"kind": "systemd", "ok": True, "path": str(unit), "manual": manual, "note": "; ".join(notes)}
    if osn == "darwin":
        pl = Path.home() / "Library/LaunchAgents/com.dllm.plist"
        pl.parent.mkdir(parents=True, exist_ok=True)
        pl.write_bytes(launchd_plist(cmd, env))
        uid = str(os.getuid())
        _run(["launchctl", "bootout", f"gui/{uid}", str(pl)], dry_run)
        ok, out = _run(["launchctl", "bootstrap", f"gui/{uid}", str(pl)], dry_run)
        return {"kind": "launchd", "ok": ok, "path": str(pl), "manual": manual,
                "note": "" if ok else f"launchctl falló: {out[:160]}"}
    if osn == "windows":
        ok, out = _run(schtasks_create(cmd), dry_run)
        if ok:
            _run(["schtasks", "/Run", "/TN", "DLLM"], dry_run)
        return {"kind": "schtasks", "ok": ok, "path": "Tarea programada 'DLLM'", "manual": manual,
                "note": "" if ok else f"schtasks falló: {out[:160]}"}
    return {"kind": "none", "ok": False, "path": None, "note": f"sistema no soportado: {osn}", "manual": manual}


def start_service(dry_run=False):
    """Arranca el servicio de usuario ya instalado. (ok, detalle)"""
    osn = platform.system().lower()
    if osn == "linux":
        return _run(["systemctl", "--user", "start", "dllm.service"], dry_run)
    if osn == "darwin":
        pl = Path.home() / "Library/LaunchAgents/com.dllm.plist"
        uid = str(os.getuid())
        _run(["launchctl", "bootstrap", f"gui/{uid}", str(pl)], dry_run)  # por si se paró con bootout
        return _run(["launchctl", "kickstart", f"gui/{uid}/com.dllm"], dry_run)
    if osn == "windows":
        return _run(["schtasks", "/Run", "/TN", "DLLM"], dry_run)
    return False, f"sistema no soportado: {osn}"


def stop_service(dry_run=False):
    """Para DLLM hasta el próximo inicio de sesión (o hasta `dllm open`). (ok, detalle)"""
    osn = platform.system().lower()
    if osn == "linux":
        return _run(["systemctl", "--user", "stop", "dllm.service"], dry_run)
    if osn == "darwin":
        pl = Path.home() / "Library/LaunchAgents/com.dllm.plist"
        return _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(pl)], dry_run)
    if osn == "windows":
        return _run(["schtasks", "/End", "/TN", "DLLM"], dry_run)
    return False, f"sistema no soportado: {osn}"


SHORTCUT_NAME = "DLLM"


def _shortcut_paths():
    osn = platform.system().lower()
    if osn == "windows":
        app = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Microsoft/Windows/Start Menu/Programs"
        return [Path.home() / "Desktop" / f"{SHORTCUT_NAME}.lnk", app / f"{SHORTCUT_NAME}.lnk"]
    if osn == "linux":
        return [Path.home() / ".local/share/applications/dllm.desktop"]
    return []


def install_shortcuts(pyz, dry_run=False):
    """Acceso directo «DLLM» (escritorio y menú Inicio en Windows; menú de aplicaciones en Linux) que abre el panel
    con `dllm open`: arranca DLLM si está parado y esconde la consola. Devuelve las rutas creadas."""
    osn = platform.system().lower()
    paths, made = _shortcut_paths(), []
    if not paths or dry_run or not pyz:
        return made
    py = sys.executable  # python.exe (con consola): la consola se esconde sola y reaparece si hace falta
    if osn == "windows":
        for p in paths:
            if not p.parent.is_dir():
                continue
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);$s.TargetPath=$env:PY;"
                  "$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:WD;$s.Description='Panel de DLLM';"
                  "$s.WindowStyle=7;$s.Save()")  # 7 = minimizada: ni se ve el parpadeo de la consola
            env = dict(os.environ, LNK=str(p), PY=py, ARGS=f'"{pyz}" open --hide-console', WD=str(Path(pyz).parent))
            ok, _ = (lambda r: (r.returncode == 0, r))(subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, capture_output=True,
                text=True, timeout=30))
            if ok and p.exists():
                made.append(str(p))
    elif osn == "linux":
        p = paths[0]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[Desktop Entry]\nType=Application\nName=DLLM\nComment=Panel de DLLM (Distributed LLM)\n"
                     f"Exec={shlex.quote(py)} {shlex.quote(str(pyz))} open\nTerminal=true\nCategories=Utility;\n")
        made.append(str(p))
    return made


def remove_shortcuts():
    for p in _shortcut_paths():
        try:
            p.unlink()
        except OSError:
            pass


def uninstall_service(dry_run=False):
    osn = platform.system().lower()
    if osn == "linux":
        _run(["systemctl", "--user", "disable", "--now", "dllm.service"], dry_run)
        (Path.home() / ".config/systemd/user/dllm.service").unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], dry_run)
    elif osn == "darwin":
        pl = Path.home() / "Library/LaunchAgents/com.dllm.plist"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(pl)], dry_run)
        pl.unlink(missing_ok=True)
    elif osn == "windows":
        _run(["schtasks", "/End", "/TN", "DLLM"], dry_run)
        _run(["schtasks", "/Delete", "/TN", "DLLM", "/F"], dry_run)


FW_PREFIX = "DLLM"


def firewall_plan(cfg):
    """Lo que hay que abrir: [(nombre, protocolo, puertos, ip_remota)]. Solo lo imprescindible:
    - nodo: API del agente y proxy RPC, aceptando solo al orquestador (o la subred local si no se restringe);
    - orquestador: beacons UDP de los nodos y, si se expone, la API; desde la subred local (o cualquiera con VPN).
    rpc-server nunca se abre: escucha solo en 127.0.0.1."""
    a, o, role = cfg["agent"], cfg["orchestrator"], cfg["role"]
    remote = [ip for ip in (a.get("allow_ip") or []) if ip not in ("127.0.0.1", "::1")]
    lan = "any" if cfg.get("advertise_host") else "LocalSubnet"
    out = []
    if role == "node":
        out.append((f"{FW_PREFIX}-agente", "TCP", f"{a['port']},{a['rpc_port']}", ",".join(remote) or "LocalSubnet"))
    if role in ("orchestrator", "both"):
        out.append((f"{FW_PREFIX}-beacon", "UDP", str(o["beacon_port"]), lan))
        if o["host"] != "127.0.0.1":
            out.append((f"{FW_PREFIX}-API", "TCP", str(o["port"]), lan))
    return out


def _fw_programs():
    """Ejecutables de Python que escucharán (el servicio usa pythonw.exe; `dllm run` a mano, python.exe)."""
    py = Path(sys.executable)
    progs = [py.with_name("pythonw.exe"), py.with_name("python.exe"), py]
    seen, out = set(), []
    for p in progs:
        if p.exists() and str(p).lower() not in seen:
            seen.add(str(p).lower())
            out.append(str(p))
    return out


def netsh_rules(cfg, programs=None):
    """Argumentos de netsh (Windows) para cada regla: por programa, puerto e IP remota, en todos los perfiles."""
    programs = programs if programs is not None else _fw_programs()
    rules = []
    for name, proto, ports, remote in firewall_plan(cfg):
        for prog in programs or [None]:
            r = ["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in", "action=allow",
                 f"protocol={proto}", f"localport={ports}", f"remoteip={remote}", "profile=any", "enable=yes"]
            if prog:
                r.append(f"program={prog}")
            rules.append(r)
    return rules


def _ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def firewall_script(cfg, programs=None):
    """Script de PowerShell que borra las reglas DLLM anteriores y crea las nuevas (se ejecuta como administrador)."""
    lines = ["$ErrorActionPreference='Continue'"]
    for name in sorted({n for n, *_ in firewall_plan(cfg)} | {f"{FW_PREFIX}-agente", f"{FW_PREFIX}-beacon",
                                                               f"{FW_PREFIX}-API", "DLLM agent", "DLLM beacon"}):
        lines.append(f"& netsh advfirewall firewall delete rule {_ps_quote('name=' + name)} | Out-Null")
    for r in netsh_rules(cfg, programs):
        lines.append("& netsh " + " ".join(_ps_quote(x) for x in r[1:]) + " | Out-Null")
    return "\n".join(lines)


def _run_elevated_ps(script, timeout=180):
    """Ejecuta un script de PowerShell como administrador (una sola confirmación de UAC). (ok, detalle)"""
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    outer = ("try{$p=Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
             f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{enc}';exit $p.ExitCode}}"
             "catch{[Console]::Error.Write($_.Exception.Message);exit 1223}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", outer],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, str(e)
    if r.returncode == 1223:
        return False, "no se concedió permiso de administrador"
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def firewall_rule_exists(name):
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def open_firewall(cfg, dry_run=False):
    """Abre en el cortafuegos de Windows solo lo necesario (pide permiso de administrador una vez).
    En Linux y macOS no se toca nada (hace falta sudo): se devuelven los comandos. {ok, auto, detail}"""
    plan = firewall_plan(cfg)
    if not plan:
        return {"ok": True, "auto": False, "detail": "no hace falta abrir nada"}
    if platform.system().lower() != "windows":
        return {"ok": False, "auto": False, "detail": "ábrelo a mano con los comandos indicados"}
    if dry_run:
        return {"ok": True, "auto": True, "detail": "(simulado)"}
    ok, d = _run_elevated_ps(firewall_script(cfg))
    missing = [n for n, *_ in plan if not firewall_rule_exists(n)]
    if missing:
        return {"ok": False, "auto": True, "detail": f"{d or 'no se crearon las reglas'}: " + ", ".join(missing)}
    return {"ok": True, "auto": True,
            "detail": "; ".join(f"{n}: {pr} {po} desde {rm}" for n, pr, po, rm in plan)}


def close_firewall(dry_run=False):
    """Quita las reglas DLLM del cortafuegos de Windows (al desinstalar)."""
    if platform.system().lower() != "windows" or dry_run:
        return
    names = [f"{FW_PREFIX}-agente", f"{FW_PREFIX}-beacon", f"{FW_PREFIX}-API", "DLLM agent", "DLLM beacon"]
    if any(firewall_rule_exists(n) for n in names):
        _run_elevated_ps("\n".join(f"& netsh advfirewall firewall delete rule {_ps_quote('name=' + n)} | Out-Null"
                                   for n in names))


def uninstall_all(cfg=None, purge=True, models=False, dry_run=False):
    """Desinstala DLLM de este equipo (agente, orquestador o ambos). Devuelve la lista de lo hecho.
    - siempre: para el programa y quita el arranque automático y las reglas del cortafuegos;
    - purge: borra llama.cpp descargado y la configuración (~/.dllm: secreto, ajustes, programa);
    - models: borra también la carpeta de modelos, pero SOLO si es la que creó DLLM (<carpeta de DLLM>/models)."""
    cfg = cfg if cfg is not None else (config.load() or {})
    done = []
    uninstall_service(dry_run)
    if not dry_run:
        remove_shortcuts()
    done.append("programa parado y arranque automático y accesos directos eliminados")
    if platform.system().lower() == "windows":
        close_firewall(dry_run)
        done.append("reglas del cortafuegos eliminadas")
    if not purge:
        return done
    idir = Path(cfg["install_dir"]).resolve() if cfg.get("install_dir") else None
    mdir = Path(cfg["models_dir"]).resolve() if cfg.get("models_dir") else None
    targets = [config.llama_dir(cfg), config.home()]
    if models and mdir:
        if idir and mdir == idir / "models":
            targets.append(mdir)
        else:
            done.append(f"modelos conservados en {mdir}: no es una carpeta creada por DLLM, bórrala a mano si quieres")
    elif mdir and cfg.get("role") != "node":
        done.append(f"modelos conservados en {mdir}")
    for t in targets:
        if Path(t).exists():
            if not dry_run:
                shutil.rmtree(t, ignore_errors=True)
            done.append(f"borrado {t}" if dry_run or not Path(t).exists() else f"no se pudo borrar del todo {t}")
    if idir and idir.is_dir() and not dry_run:
        try:
            idir.rmdir()  # solo si ha quedado vacía
            done.append(f"borrado {idir}")
        except OSError:
            pass
    return done


def firewall_hints(cfg):
    """Comandos equivalentes para abrirlo a mano (Windows: los que ejecuta open_firewall)."""
    osn, out = platform.system().lower(), []
    plan = firewall_plan(cfg)
    if osn == "linux":
        for name, proto, ports, remote in plan:
            for port in ports.split(","):
                srcs = [x for x in remote.split(",") if x not in ("LocalSubnet", "any")]
                for src in srcs or [None]:
                    out.append(f"sudo ufw allow {'from ' + src + ' to any ' if src else ''}"
                               f"{'port ' + port + ' proto ' + proto.lower() if src else port + '/' + proto.lower()}")
    elif osn == "windows":
        out = [" ".join(f'"{x}"' if " " in x else x for x in r) for r in netsh_rules(cfg)]
    elif osn == "darwin" and plan:
        out.append("(macOS) si aparece el aviso 'permitir conexiones entrantes' para Python, pulsa Permitir")
    return out


# ============================================================ diagnóstico
def check_local(cfg, timeout=25):
    """Espera a que el programa recién arrancado responda en este equipo. Devuelve (ok, detalle)."""
    import http.client
    from . import orchestrator
    deadline, last = time.time() + timeout, "sin respuesta"
    while time.time() < deadline:
        try:
            if cfg["role"] in ("orchestrator", "both"):
                c = http.client.HTTPConnection("127.0.0.1", cfg["orchestrator"]["port"], timeout=2)
                c.request("GET", "/healthz")
                if c.getresponse().status == 200:
                    return True, f"orquestador activo en el puerto {cfg['orchestrator']['port']}"
            elif not cfg.get("secret"):  # nodo sin emparejar: basta con que responda su emparejamiento
                c = http.client.HTTPConnection("127.0.0.1", cfg["agent"]["port"], timeout=2)
                c.request("GET", "/pair/local")
                if c.getresponse().status == 200:
                    return True, "agente activo, esperando a que un orquestador lo reclame"
            else:
                node = orchestrator.Node("127.0.0.1", cfg["agent"]["port"])
                info = orchestrator.call_agent(cfg["secret"], node, "GET", "/info", timeout=2)
                return True, f"agente activo ({info.get('backend')}, {info.get('cap_bytes', 0) / hw.GiB:.1f} GiB)"
        except Exception as e:
            last = str(e)[:100]
        time.sleep(1)
    return False, last


def doctor(cfg):
    """Lista de {name, ok, detail} con comprobaciones locales."""
    out = []
    add = lambda n, ok, d="": out.append({"name": n, "ok": ok, "detail": d})
    add("Python ≥ 3.9", sys.version_info >= (3, 9), platform.python_version())
    sac = hw.smart_app_control()
    if sac in ("on", "eval"):
        add("Control inteligente de aplicaciones", False,
            ("activado" if sac == "on" else "en evaluación") + ": puede bloquear las DLL sin firmar de llama.cpp")
    if not cfg:
        add("Configuración", False, "no hay config.json: ejecuta `dllm setup`")
        return out
    role = cfg["role"]
    add("Configuración", True, f"rol {role}, nombre {cfg['name']}")
    need = {"orchestrator": ["llama-server"], "node": ["rpc-server"], "both": ["llama-server", "rpc-server"]}[role]
    builds = set()
    for n in need:
        p = bin_path(cfg["bin_dir"], n)
        ok, txt = run_check(p) if p.is_file() else (False, "no existe")
        add(n, ok, txt.splitlines()[0] if txt else "")
        if ok:
            builds.add(hw.build_id(str(p)))
    if len(builds) > 1:
        add("Misma build", False, f"llama-server y rpc-server difieren: {builds}")
    ok, d = check_local(cfg, timeout=2)
    add("Programa en marcha", ok, d if ok else "no responde (¿servicio parado? prueba `dllm run`)")
    return out
