# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Telemetría del nodo en tiempo real, sin dependencias externas y para los fabricantes habituales.

Fuente de los datos de GPU, por orden de preferencia:
  - NVIDIA (Windows/Linux): `nvidia-smi` → uso, VRAM, temperatura, consumo y límite, ventilador, reloj.
  - AMD en Linux: sysfs del driver amdgpu → `gpu_busy_percent`, `mem_info_vram_*`, hwmon (temp1_input en
    milésimas de °C; power1_average/power1_input y power1_cap en µW; fan1_input en RPM).
  - Windows (cualquier fabricante con WDDM 2.x, p. ej. AMD o Intel): contadores PDH del Administrador de tareas
    («GPU Engine» y «GPU Adapter Memory») → uso y VRAM dedicada. Windows no expone temperatura ni consumo sin el
    SDK propietario de cada fabricante. Se usan los nombres en inglés (PdhAddEnglishCounterW) para que funcione en
    Windows en cualquier idioma.
  - Apple: `ioreg` (IOAccelerator) → uso y memoria en uso.
  - Si nada de lo anterior está disponible: la memoria que informa llama.cpp.
CPU: /proc/stat (Linux), GetSystemTimes (Windows), carga media (macOS, aproximado). RAM: hw.mem_info.
"""
import csv
import ctypes
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import hw

NVIDIA_FIELDS = ("index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,"
                 "fan.speed,clocks.sm")
MiB = 1 << 20


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None  # "[N/A]", "[Not Supported]"…


# ============================================================ NVIDIA
def parse_nvidia_smi(text):
    out = []
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) < 10:
            continue
        r = [c.strip() for c in row]
        used, total = _num(r[3]), _num(r[4])
        out.append({"index": r[0], "name": r[1], "util": _num(r[2]),
                    "vram_used": int(used * MiB) if used is not None else None,
                    "vram_total": int(total * MiB) if total is not None else None,
                    "temp": _num(r[5]), "power": _num(r[6]), "power_limit": _num(r[7]),
                    "fan": _num(r[8]), "clock_mhz": _num(r[9]), "source": "nvidia-smi"})
    return out


_WHICH = {}


def _which(name, ttl=300):
    """shutil.which recorre todo el PATH: se cachea unos minutos (se usa cada 2 s)."""
    k = (name, os.environ.get("PATH"))
    v = _WHICH.get(k)
    if v is None or time.time() - v[1] > ttl:
        v = _WHICH[k] = (shutil.which(name), time.time())
    return v[0]


def nvidia_smi_once():
    exe = _which("nvidia-smi")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, f"--query-gpu={NVIDIA_FIELDS}", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        return parse_nvidia_smi(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return []


class NvidiaStream:
    """Un único `nvidia-smi -lms` en marcha en lugar de lanzar el programa cada 2 s (cada arranque inicializa NVML y
    cuesta decenas de ms de CPU). Si el proceso muere o deja de escribir, se vuelve a la consulta puntual; tras varios
    fallos seguidos se queda en modo puntual."""

    def __init__(self, interval_ms=2000):
        self.interval = interval_ms / 1000
        self.proc, self.latest, self.ts, self.fails, self.exe = None, {}, 0.0, 0, None
        self.lock = threading.Lock()

    def _start(self, exe):
        try:
            self.proc = subprocess.Popen([exe, f"--query-gpu={NVIDIA_FIELDS}", "--format=csv,noheader,nounits",
                                          f"-lms={int(self.interval * 1000)}"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError:
            self.proc, self.fails = None, self.fails + 1
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                for g in parse_nvidia_smi(line):
                    with self.lock:
                        self.latest[g["index"]] = g
                        self.ts = time.time()
        except (OSError, ValueError):
            pass
        self.fails += 1

    def read(self):
        exe = _which("nvidia-smi")
        if not exe:
            return []
        if exe != self.exe:  # otro nvidia-smi (PATH distinto): se empieza de cero
            self.stop()
            with self.lock:
                self.proc, self.latest, self.ts, self.fails, self.exe = None, {}, 0.0, 0, exe
        if self.fails < 3 and (self.proc is None or self.proc.poll() is not None):
            self._start(exe)
        with self.lock:
            fresh = time.time() - self.ts < 3 * self.interval + 1
            cur = sorted(self.latest.values(), key=lambda g: g["index"]) if fresh else None
        return cur if cur is not None else nvidia_smi_once()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


_STREAM = NvidiaStream()


def nvidia_smi():
    return _STREAM.read()


# ============================================================ AMD (Linux, sysfs)
def _read(p, scale=1.0):
    try:
        return float(Path(p).read_text().strip()) / scale
    except (OSError, ValueError):
        return None


def amdgpu_sysfs(root=None):
    root = Path(root or os.environ.get("DLLM_SYSFS_DRM", "/sys/class/drm"))
    out = []
    if not root.is_dir():
        return out
    for card in sorted(root.glob("card[0-9]*")):
        dev = card / "device"
        if "-" in card.name or not (dev / "vendor").exists():
            continue  # card0-DP-1 y similares son conectores, no GPUs
        try:
            if (dev / "vendor").read_text().strip().lower() != "0x1002":
                continue
        except OSError:
            continue
        total = _read(dev / "mem_info_vram_total")
        if not total:
            continue
        g = {"index": card.name, "name": "AMD GPU", "util": _read(dev / "gpu_busy_percent"),
             "vram_used": int(_read(dev / "mem_info_vram_used") or 0), "vram_total": int(total),
             "temp": None, "power": None, "power_limit": None, "fan": None, "clock_mhz": None, "source": "amdgpu"}
        for hm in sorted((dev / "hwmon").glob("hwmon*")):
            g["temp"] = _read(hm / "temp1_input", 1000)
            g["power"] = _read(hm / "power1_average", 1e6)
            if g["power"] is None:
                g["power"] = _read(hm / "power1_input", 1e6)
            g["power_limit"] = _read(hm / "power1_cap", 1e6)
            g["fan"] = _read(hm / "fan1_input")
            break
        out.append(g)
    return out


# ============================================================ Windows (PDH, cualquier fabricante)
def aggregate_wddm(engine_items, memory_items):
    """engine_items: {instancia: % uso} de «GPU Engine»; memory_items: {instancia: bytes} de «GPU Adapter Memory».
    Como el Administrador de tareas: uso de una GPU = el motor más ocupado (sumando procesos)."""
    per_engine = {}
    for name, v in engine_items.items():
        m = re.search(r"(luid_0x[0-9a-fA-F]+_0x[0-9a-fA-F]+).*?(eng_\d+)", name)
        if m and v is not None:
            per_engine[(m.group(1).lower(), m.group(2))] = per_engine.get((m.group(1).lower(), m.group(2)), 0) + v
    util = {}
    for (luid, _), v in per_engine.items():
        util[luid] = max(util.get(luid, 0), v)
    mem = {}
    for name, v in memory_items.items():
        m = re.search(r"(luid_0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", name)
        if m and v is not None:
            mem[m.group(1).lower()] = mem.get(m.group(1).lower(), 0) + v
    return {luid: {"util": min(100.0, util.get(luid, 0.0)), "vram_used": int(mem.get(luid, 0))}
            for luid in set(util) | set(mem)}


class _Pdh:
    """Consulta PDH persistente (los contadores de % necesitan dos lecturas; se mantiene la consulta abierta)."""
    PDH_FMT_DOUBLE, PDH_FMT_NOCAP100, PDH_MORE_DATA = 0x200, 0x8000, 0x800007D2

    def __init__(self):
        from ctypes import wintypes
        self.w = wintypes
        self.pdh = ctypes.WinDLL("pdh")

        class VALUE(ctypes.Structure):
            class U(ctypes.Union):
                _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                            ("largeValue", ctypes.c_longlong), ("str", ctypes.c_void_p)]
            _fields_ = [("CStatus", wintypes.DWORD), ("u", U)]

        class ITEM(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", VALUE)]

        self.ITEM = ITEM
        self.query = ctypes.c_void_p()
        if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) != 0:
            raise OSError("PdhOpenQueryW")
        self.engine, self.memory = ctypes.c_void_p(), ctypes.c_void_p()
        for path, h in ((r"\GPU Engine(*)\Utilization Percentage", self.engine),
                        (r"\GPU Adapter Memory(*)\Dedicated Usage", self.memory)):
            if self.pdh.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(h)) != 0:
                raise OSError(f"contador no disponible: {path}")
        self.pdh.PdhCollectQueryData(self.query)

    def _array(self, counter):
        size, count = self.w.DWORD(0), self.w.DWORD(0)
        fmt = self.PDH_FMT_DOUBLE | self.PDH_FMT_NOCAP100
        # PDH devuelve un DWORD; ctypes lo lee como int con signo (0x800007D2 sale negativo): se normaliza
        rc = self.pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), None) & 0xFFFFFFFF
        if rc != self.PDH_MORE_DATA:
            return {}
        buf = (ctypes.c_byte * size.value)()
        rc = self.pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if rc != 0:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(self.ITEM))
        return {items[i].szName: items[i].FmtValue.u.doubleValue for i in range(count.value)
                if items[i].FmtValue.CStatus in (0, 1)}  # PDH_CSTATUS_VALID_DATA / NEW_DATA

    def sample(self):
        if self.pdh.PdhCollectQueryData(self.query) != 0:
            return {}
        return aggregate_wddm(self._array(self.engine), self._array(self.memory))


# ============================================================ Apple
def parse_ioreg(text):
    util = re.search(r'"Device Utilization %"\s*=\s*(\d+)', text or "")
    used = re.search(r'"In use system memory"\s*=\s*(\d+)', text or "")
    if not util and not used:
        return None
    return {"util": float(util.group(1)) if util else None, "vram_used": int(used.group(1)) if used else None}


def ioreg():
    try:
        r = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                           capture_output=True, text=True, timeout=5)
        return parse_ioreg(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return None


# ============================================================ CPU
class _Cpu:
    def __init__(self):
        self.prev = self._times()

    def _times(self):
        try:
            if sys.platform.startswith("linux"):
                with open("/proc/stat") as f:
                    v = [int(x) for x in f.readline().split()[1:]]
                idle = v[3] + (v[4] if len(v) > 4 else 0)
                return idle, sum(v)
            if os.name == "nt":
                from ctypes import wintypes
                i, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
                ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
                ft = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
                return ft(i), ft(k) + ft(u)  # el tiempo de kernel incluye el idle
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def percent(self):
        cur = self._times()
        if cur is None:
            try:  # macOS y otros: carga media como aproximación
                return min(100.0, os.getloadavg()[0] * 100 / (os.cpu_count() or 1))
            except (OSError, AttributeError):
                return None
        if self.prev is None:
            self.prev = cur
            return None
        di, dt = cur[0] - self.prev[0], cur[1] - self.prev[1]
        self.prev = cur
        return round(100.0 * (1 - di / dt), 1) if dt > 0 else None


# ============================================================ muestreador
def _match(cands, device):
    """La GPU de la lista que corresponde al dispositivo que presta el nodo."""
    if not cands:
        return None
    if device:
        d = (device.get("desc") or "").lower()
        for c in cands:
            if c.get("name") and c["name"].lower() in d or d and d in (c.get("name") or "").lower():
                return c
        tot = device.get("total") or 0
        return min(cands, key=lambda c: abs((c.get("vram_total") or 0) - tot))
    return max(cands, key=lambda c: c.get("vram_total") or 0)


class Sampler:
    def __init__(self, device_fn, interval=None):
        self.device_fn = device_fn  # devuelve el dispositivo elegido por el agente (o None)
        self.interval = interval or (3.0 if os.name == "nt" else 2.0)
        self.cpu = _Cpu()
        self.latest = {}
        self.rx = self.tx = 0
        self._last = (time.time(), 0, 0)
        self._pdh = None
        self._pdh_failed = False
        self.lock = threading.Lock()

    def add_bytes(self, direction, n):
        with self.lock:
            if direction == "rx":
                self.rx += n
            else:
                self.tx += n

    def _gpu(self, device):
        if not device:  # llama.cpp no usa ninguna GPU, pero si el equipo tiene una se muestra (sin prestarla)
            g = _match(nvidia_smi(), None)
            return dict(g, unused=True) if g else None
        g = _match(nvidia_smi(), device)
        if g is None and sys.platform.startswith("linux"):
            g = _match(amdgpu_sysfs(), device)
        if g is None and os.name == "nt" and not self._pdh_failed:
            try:
                self._pdh = self._pdh or _Pdh()
                per = self._pdh.sample()
                if per:
                    best = max(per.values(), key=lambda x: x["vram_used"])  # la GPU dedicada es la que tiene VRAM
                    g = {"util": best["util"], "vram_used": best["vram_used"], "source": "windows"}
            except (OSError, AttributeError, ValueError):
                self._pdh_failed = True
        if g is None and sys.platform == "darwin":
            r = ioreg()
            if r:
                g = dict(r, source="ioreg")
        g = dict(g or {"source": "llama.cpp"})
        g.setdefault("name", device.get("desc"))
        if g.get("name") in (None, "AMD GPU"):
            g["name"] = device.get("desc")
        if not g.get("vram_total"):
            g["vram_total"] = device.get("total")
        if g.get("vram_used") is None and device.get("total") and device.get("free") is not None:
            g["vram_used"] = device["total"] - device["free"]
        for k in ("util", "temp", "power", "power_limit", "fan", "clock_mhz"):
            g.setdefault(k, None)
        return g

    def sample(self):
        now = time.time()
        with self.lock:
            rx, tx = self.rx, self.tx
        t0, rx0, tx0 = self._last
        dt = max(1e-3, now - t0)
        self._last = (now, rx, tx)
        total, free = hw.mem_info()
        s = {"t": now, "cpu": self.cpu.percent(), "ram_used": total - free if total else None, "ram_total": total,
             "rpc_rx_bps": int((rx - rx0) / dt), "rpc_tx_bps": int((tx - tx0) / dt)}
        try:
            s["gpu"] = self._gpu(self.device_fn())
        except Exception:
            s["gpu"] = None
        self.latest = s
        return s

    def loop(self):
        while True:
            try:
                self.sample()
            except Exception:
                pass
            time.sleep(self.interval)
