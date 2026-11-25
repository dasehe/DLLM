# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Sondeo de hardware del nodo: memoria, GPU NVIDIA, ancho de banda de memoria estimado."""
import ctypes
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time

GiB = 1 << 30
GPU_BW_DEFAULT = 300.0  # GB/s, heurística para GPU discreta; se puede forzar con --bw-gbps


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


SLOT_PORTS = (18081, 18180)  # llama-server de cada slot: puertos internos consecutivos, solo 127.0.0.1


def slot_port(taken=()):
    """Primer puerto libre del rango de slots (18081…); si el rango está lleno, uno efímero."""
    for p in range(SLOT_PORTS[0], SLOT_PORTS[1] + 1):
        if p in taken:
            continue
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            continue
        finally:
            s.close()
        return p
    return free_port()


def quiet_windows_errors():
    """Windows: que un fallo al cargar una DLL en un proceso hijo (llama-server, rpc-server) devuelva un código de
    error en vez de abrir un diálogo modal que lo deja colgado. Los hijos heredan el modo de error."""
    if os.name == "nt":
        try:  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        except Exception:
            pass


CREATE_NO_WINDOW = 0x08000000


def hide_child_consoles():
    """Windows: el servicio corre con pythonw (sin consola) y cada programa de consola que lanza (rpc-server,
    llama-server, nvidia-smi…) abriría su propia ventana de CMD. Se lanzan todos sin ventana; las ventanas
    gráficas (selector de carpetas) se siguen viendo. La salida se recoge igual por tuberías."""
    if os.name != "nt" or getattr(subprocess.Popen, "_dllm_no_window", False):
        return
    orig = subprocess.Popen.__init__

    def __init__(self, *args, **kw):
        kw["creationflags"] = (kw.get("creationflags") or 0) | CREATE_NO_WINDOW
        orig(self, *args, **kw)

    subprocess.Popen.__init__ = __init__
    subprocess.Popen._dllm_no_window = True


def smart_app_control():
    """Estado del Control inteligente de aplicaciones de Windows 11: 'on', 'eval', 'off' o None (no aplica)."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as k:
            v = winreg.QueryValueEx(k, "VerifiedAndReputablePolicyState")[0]
        return {0: "off", 1: "on", 2: "eval"}.get(v)
    except Exception:
        return None


# llama.cpp renombró rpc-server a ggml-rpc-server (tools/rpc); se aceptan ambos nombres.
BIN_ALIASES = {"rpc-server": ("ggml-rpc-server", "rpc-server")}


def bin_names(name):
    """Nombres posibles (sin extensión) de un ejecutable de llama.cpp, del más reciente al más antiguo."""
    return BIN_ALIASES.get(name, (name,))


def find_bin(name, bin_dir=None):
    ext = ".exe" if os.name == "nt" else ""
    for d in (bin_dir, os.environ.get("DLLM_BIN_DIR")):
        if d:
            for n in bin_names(name):
                p = os.path.join(d, n + ext)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
    for n in bin_names(name):
        p = shutil.which(n + ext)
        if p:
            return p
    return None


def _version_of(binary):
    try:
        # llama-server --version carga todos los backends (Vulkan, CUDA…): en Windows puede tardar
        r = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=60)
        m = re.search(r"version:\s*(\d+)\s*\(([^)]+)\)", r.stdout + r.stderr)
        return f"{m.group(1)} ({m.group(2)})" if m else None
    except Exception:
        return None


def is_rpc_server(binary):
    stem = os.path.basename(str(binary)).lower()
    stem = stem[:-4] if stem.endswith(".exe") else stem
    return stem in bin_names("rpc-server")


def build_id(binary):
    """'1234 (abcdef)'. rpc-server y llama-server deben ser de la MISMA build.
    rpc-server no tiene --version: se pregunta a llama-server (o llama-cli) de su misma carpeta, que viene en
    el mismo paquete de llama.cpp."""
    if not is_rpc_server(binary):
        return _version_of(binary)
    ext = ".exe" if os.name == "nt" else ""
    d = os.path.dirname(os.path.abspath(str(binary)))
    for sib in ("llama-server", "llama-cli"):
        p = os.path.join(d, sib + ext)
        if os.path.isfile(p):
            b = _version_of(p)
            if b:
                return b
    b = _version_of(binary)  # por si alguna build futura lo admite
    if b:
        return b
    # último recurso: la carpeta que crea el instalador se llama llama-b12387-<backend>
    m = re.search(r"llama-b(\d+)-", d.replace("\\", "/"))
    return f"{m.group(1)} (?)" if m else None


def mem_info():
    """(total, disponible) en bytes."""
    try:
        if sys.platform.startswith("linux"):
            d = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    d[k] = int(v.split()[0]) * 1024
            return d["MemTotal"], d.get("MemAvailable", d.get("MemFree", 0))
        if sys.platform == "darwin":
            total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]))
            out = subprocess.check_output(["vm_stat"], text=True)
            ps = int(re.search(r"page size of (\d+)", out).group(1))
            pages = 0
            for k in ("free", "inactive", "speculative"):
                m = re.search(rf"Pages {k}:\s+(\d+)", out)
                pages += int(m.group(1)) if m else 0
            return total, pages * ps
        if os.name == "nt":
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return ms.ullTotalPhys, ms.ullAvailPhys
    except Exception:
        pass
    return 0, 0


def nvidia_gpus():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        gpus = []
        for line in r.stdout.strip().splitlines():
            name, tot, free = [x.strip() for x in line.split(",")]
            gpus.append({"name": name, "total": int(tot) * (1 << 20), "free": int(free) * (1 << 20)})
        return gpus
    except Exception:
        return []


def cpu_name():
    try:
        if sys.platform.startswith("linux"):
            for line in open("/proc/cpuinfo"):
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def cpu_bw_estimate(size=48 << 20, iters=4):
    """Ancho de banda de memoria (GB/s) por memcpy de un hilo, escalado ×min(núcleos,2).
    Es una HEURÍSTICA para ordenar nodos; se puede forzar con --bw-gbps."""
    a, b = bytearray(size), bytearray(size)
    ma, mb = memoryview(a), memoryview(b)
    best = 1e9
    for _ in range(iters):
        t = time.perf_counter()
        mb[:] = ma
        best = min(best, time.perf_counter() - t)
    return 2 * size / best / 1e9 * min(os.cpu_count() or 1, 2)


class Probe:
    def __init__(self, backend="auto", reserve_gb=1.0, bw_gbps=None, gpu_index=0):
        auto = backend == "auto"
        self.cores = os.cpu_count() or 1
        self.cpu = cpu_name()
        self.reserve = int(reserve_gb * GiB)
        self.bw_fixed = bw_gbps is not None
        self.gpu_index = gpu_index
        gpus = nvidia_gpus()
        if backend == "auto":
            backend = "gpu" if gpus else "cpu"
        if backend == "gpu" and not gpus:
            print("aviso: --backend gpu pero no hay nvidia-smi/GPU; usando cpu", file=sys.stderr)
            backend = "cpu"
        self.backend = backend
        self.auto_backend = auto
        self.bw = bw_gbps or (GPU_BW_DEFAULT if backend == "gpu" else cpu_bw_estimate())

    def set_device(self, dev):
        """Acelerador elegido tras preguntar a llama.cpp (cualquier fabricante). None = se presta RAM/CPU."""
        self.device = dev
        if dev:
            self.backend = "gpu"
            if not self.bw_fixed:
                self.bw = GPU_BW_DEFAULT
        elif self.backend == "gpu" and self.auto_backend:
            # nvidia-smi veía una GPU, pero llama.cpp no ofrece ningún acelerador: la capacidad real es CPU/RAM
            self.backend = "cpu"
            if not self.bw_fixed:
                self.bw = cpu_bw_estimate()

    def hardware(self):
        total, avail = mem_info()
        gpu = None
        dev = getattr(self, "device", None)
        if dev:
            gpu = {"name": dev["desc"], "total": dev["total"], "free": dev["free"], "device": dev["name"]}
        elif self.backend == "gpu":
            gpus = nvidia_gpus()
            gpu = gpus[min(self.gpu_index, len(gpus) - 1)] if gpus else None
        return {"cores": self.cores, "ram_total": total, "ram_free": avail, "gpu": gpu, "reserve": self.reserve}

    def snapshot(self, lim=None):
        """Estado del nodo con los límites aplicados (lim: {share, mem_gb, threads})."""
        from . import limits
        h = self.hardware()
        eff = limits.resolve(lim, h)
        bw = self.bw
        if not self.bw_fixed and self.backend == "cpu":
            # menos hilos ⇒ menos ancho de banda de memoria aprovechable (heurística lineal hasta ~núcleos físicos)
            bw *= min(1.0, eff["threads"] / max(1, self.cores // 2))
        return {"os": f"{platform.system()} {platform.release()}", "cpu": self.cpu, "cores": self.cores,
                "ram_total": h["ram_total"], "ram_free": h["ram_free"], "backend": self.backend, "gpu": h["gpu"],
                "reserve": self.reserve, "cap_bytes": eff["cap_bytes"], "threads": eff["threads"],
                "mem_source": eff["mem_source"], "threads_source": eff["threads_source"],
                "recommended": eff["recommended"], "ceiling": eff["ceiling"], "bw_gbps": round(bw, 1)}
