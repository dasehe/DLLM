# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Asistente de instalación. Un núcleo (`Setup`) y dos frontends: terminal y navegador (clic).

El frontend web es un servidor LOCAL (127.0.0.1) protegido con token aleatorio y comprobación de Host/Origin,
porque puede instalar un servicio en el equipo: ninguna otra página o usuario debe poder manejarlo.
"""
import json
import os
import pkgutil
import re
import secrets
import socket
import subprocess
import shutil
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, auth, config, devices, hw, installer, legal_notice, limits, presence
from .util import FastHTTPServer

ROLES = ("orchestrator", "node", "both")
BACKENDS = ("auto", "cpu", "vulkan", "cuda", "rocm", "sycl", "metal")
STEPS = ["Preparación", "Instalar llama.cpp", "Guardar configuración", "Copiar programa",
         "Arranque automático", "Comprobación"]
NEED = {"orchestrator": ["llama-server"], "node": ["rpc-server"], "both": ["llama-server", "rpc-server"]}


def _port(v, default, what):
    try:
        p = int(v if v not in (None, "") else default)
    except (TypeError, ValueError):
        raise ValueError(f"puerto de {what} no válido")
    if not 1 <= p <= 65535:
        raise ValueError(f"puerto de {what} fuera de rango")
    return p


def _num(v, what):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} debe ser un número")


class Setup:
    def __init__(self, emit=None, dry_run=False):
        self.cb, self.dry = emit, dry_run
        self.events, self.lock = [], threading.Lock()
        self.running = self.done = False
        self.error = self.result = None

    # ------------------------------------------------------------ eventos
    def emit(self, step, status, detail="", label=None):
        ev = {"step": step, "total": len(STEPS), "label": label or STEPS[step], "status": status,
              "detail": detail, "t": time.time()}
        with self.lock:
            ev["i"] = len(self.events)
            self.events.append(ev)
        if self.cb:
            self.cb(ev)

    # ------------------------------------------------------------ detección
    def detect(self):
        info = installer.platform_info()
        sug, alts = installer.suggest_backends(info)
        cfg = config.load()
        h = {"cores": info["cores"], "ram_total": info["ram_total"], "ram_free": info["ram_free"], "gpu": None,
             "reserve": hw.GiB}
        rec = limits.recommend(h)
        return {"version": __version__, "platform": info, "suggested_backend": sug, "backends": alts,
                "backend_notes": {b: installer.BACKEND_NOTES.get(b, "") for b in alts},
                "recommended": {"mem_gb": round(rec["mem_bytes"] / hw.GiB, 1), "threads": rec["threads"]},
                "lan_ip": config.lan_ip(), "hostname": socket.gethostname(),
                "default_install_dir": config.default_install_dir(), "existing": config.redacted(cfg),
                "pyz": installer.running_from_pyz()}

    # ------------------------------------------------------------ validación
    def normalize(self, o):
        role = o.get("role")
        if role not in ROLES:
            raise ValueError("elige qué quieres instalar: orquestador, nodo o ambos")
        n = {"role": role, "join": None}
        n["name"] = re.sub(r"[^\w.\-]", "-", (o.get("name") or socket.gethostname()).strip())[:40] or "nodo"
        if role == "node" and (o.get("join_code") or "").strip():
            n["join"] = config.decode_join(o.get("join_code"))
        # nodo sin código: se instala igual y espera a que un orquestador lo reclame (pairing.py)
        n["install_dir"] = os.path.abspath(os.path.expanduser(o.get("install_dir") or config.default_install_dir()))
        n["models_dir"] = os.path.abspath(os.path.expanduser(o.get("models_dir")
                                                             or os.path.join(n["install_dir"], "models")))
        n["firewall"] = bool(o.get("firewall", True))
        n["agent_port"] = _port(o.get("agent_port"), 8765, "agente")
        n["rpc_port"] = _port(o.get("rpc_port"), 50051, "RPC")
        n["api_port"] = _port(o.get("api_port"), 8080, "API")
        n["beacon_port"] = _port(o.get("beacon_port"), 50999, "descubrimiento")
        n["backend"] = o.get("backend") or "auto"
        if n["backend"] not in BACKENDS:
            raise ValueError(f"backend desconocido: {n['backend']}")
        n["bin_dir"] = o.get("bin_dir") or None  # carpeta con llama.cpp ya instalado
        n["expose"] = bool(o.get("expose"))
        n["autostart"] = bool(o.get("autostart", True))
        n["restrict_ip"] = bool(o.get("restrict_ip", True))
        n["advertise"] = o.get("advertise_host") or None
        n["mem_gb"] = _num(o.get("mem_gb"), "la memoria")
        n["share"] = _num(o.get("share"), "el porcentaje")
        if n["share"] is not None and not 1 <= n["share"] <= 100:
            raise ValueError("el porcentaje a compartir debe estar entre 1 y 100")
        t = _num(o.get("threads"), "los hilos")
        if t is not None and (t != int(t) or t < 1):
            raise ValueError("los hilos deben ser un entero mayor que 0")
        n["threads"] = int(t) if t is not None else None
        mode = o.get("limits_mode")
        if mode not in (None, "", "auto", "manual"):
            raise ValueError("modo de contribución desconocido (auto o manual)")
        # si se fija algún límite sin decir el modo, se entiende que se quiere controlar desde este equipo
        n["limits_mode"] = mode or ("manual" if any(n[k] is not None for k in ("share", "mem_gb", "threads"))
                                    else "auto")
        n["bw_gbps"] = _num(o.get("bw_gbps"), "el ancho de banda")
        n["reserve_gb"] = _num(o.get("reserve_gb"), "la reserva") or 1.0
        return n

    # ------------------------------------------------------------ instalación
    def start(self, o):
        with self.lock:
            if self.running:
                raise RuntimeError("ya hay una instalación en curso")
            self.running, self.done, self.error, self.result, self.events = True, False, None, None, []
        threading.Thread(target=self._thread, args=(o,), daemon=True).start()

    def _thread(self, o):
        try:
            self.result = self.install(o)
        except Exception as e:
            self.error = str(e)
        finally:
            self.done, self.running = True, False

    def install(self, o):
        step = 0
        try:
            self.emit(0, "run")
            n = self.normalize(o)
            role, info = n["role"], installer.platform_info()
            old = config.load()
            cfg = config.default(role)
            cfg["name"], cfg["models_dir"], cfg["install_dir"] = n["name"], n["models_dir"], n["install_dir"]
            if role == "node":
                cfg["secret"] = n["join"]["secret"] if n["join"] else None
            else:  # el orquestador conserva su secreto para no desemparejar nodos ya instalados
                cfg["secret"] = (old or {}).get("secret") or auth.new_secret()
            o_, a_ = cfg["orchestrator"], cfg["agent"]
            o_["port"], o_["beacon_port"] = n["api_port"], n["beacon_port"]
            a_.update(port=n["agent_port"], rpc_port=n["rpc_port"], reserve_gb=n["reserve_gb"],
                      mem_gb=n["mem_gb"], bw_gbps=n["bw_gbps"], share=n["share"], threads=n["threads"],
                      limits_mode=n["limits_mode"])
            backend = n["backend"]
            auto_backend = backend == "auto"
            if auto_backend:
                backend = installer.suggest_backends(info)[0]
            warn = installer.backend_warning(backend, info)
            if warn:
                self.emit(1, "warn", warn, label="Backend")
            if role == "node" and n["join"]:
                j = n["join"]
                o_["beacon_port"] = int(j["beacon"])
                a_["announce_to"] = [f"{j['orch']}:{j['beacon']}"]
                if n["restrict_ip"]:
                    a_["allow_ip"] = [j["orch"], "127.0.0.1"]
            elif role == "node":  # sin código: tras emparejarse solo aceptará al orquestador que lo reclame
                a_["restrict_ip"] = n["restrict_ip"]
            elif role == "both":
                # el orquestador ya registra el agente local por 127.0.0.1; sin broadcast evitamos un nodo duplicado
                a_["allow_ip"] = ["127.0.0.1"]
                a_["announce_to"] = [f"127.0.0.1:{n['beacon_port']}"]
            if n["advertise"]:
                cfg["advertise_host"] = n["advertise"]
            if role != "node" and n["expose"]:
                o_["host"], o_["api_key"] = "0.0.0.0", auth.new_secret()[:24]
            if not self.dry:
                try:
                    Path(n["install_dir"]).mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise RuntimeError(f"no se pudo crear la carpeta de DLLM {n['install_dir']}: {e}")
            self.emit(0, "ok", f"rol {role} · {info['os']}/{info['arch']} · carpeta {n['install_dir']}")

            # 1 · llama.cpp
            step = 1
            self.emit(1, "run")
            need = NEED[role]
            if n["bin_dir"]:
                build = installer.verify_bins(n["bin_dir"], need)
                cfg["bin_dir"], cfg["llama_tag"] = n["bin_dir"], installer.tag_from_build(build)
                self.emit(1, "ok", f"usando {n['bin_dir']} (build {build})")
            elif self.dry:
                cfg["bin_dir"] = "(simulado)"
                self.emit(1, "ok", "simulado")
            else:
                tag = (n["join"] or {}).get("tag") if role == "node" else None
                # con «auto», si el mejor backend no arranca (driver, runtime…) se prueba el siguiente
                tries = installer.fallback_backends(backend, info) if auto_backend else [backend]
                for k, b in enumerate(tries):
                    try:
                        res = installer.install_llama(
                            config.llama_dir(cfg), tag, b, info,
                            lambda pct, msg, b=b: self.emit(1, "run", f"[{b}] {msg} {pct}%"), need)
                        backend = b
                        break
                    except installer.InstallError as e:
                        # bloqueo de Windows por directiva: los demás backends llevan las mismas DLL, no se prueban
                        if k == len(tries) - 1 or isinstance(e, installer.PolicyBlocked):
                            raise
                        self.emit(1, "warn", f"{b} no disponible ({str(e)[:160]}); se prueba {tries[k + 1]}",
                                  label=f"Backend {b}")
                cfg["bin_dir"], cfg["llama_tag"] = res["bin_dir"], res["tag"]
                v = "sha256 verificado" if res["verified"] else "sin sha256 publicado"
                self.emit(1, "ok", f"llama.cpp {res['tag']} ({backend}) · {v}")
            cfg["llama_backend"] = backend  # el agente lo usa si el orquestador le pide otra versión
            # backend del agente: 'gpu' solo para CUDA; el resto en auto (el dispositivo lo detecta llama.cpp)
            a_["backend"] = "gpu" if backend == "cuda" else ("cpu" if backend == "cpu" else "auto")
            if role != "orchestrator" and not self.dry:
                # qué va a prestar este equipo, preguntando al propio llama.cpp (NVIDIA, AMD, Intel, Apple…)
                try:
                    devs = devices.probe_rpc_server(hw.find_bin("rpc-server", cfg["bin_dir"]))
                    d = devices.choose(devs)
                    self.emit(1, "ok", f"GPU detectada: {d['desc']} ({d['name']}, {d['total'] / hw.GiB:.1f} GiB)"
                              if d else "sin GPU utilizable: prestará CPU y RAM", label="Dispositivo")
                except (RuntimeError, ValueError, TypeError) as e:
                    self.emit(1, "warn", f"no se pudo detectar el dispositivo ({e})", label="Dispositivo")

            # 2 · configuración
            step = 2
            self.emit(2, "run")
            if not self.dry:
                if role != "node":
                    try:
                        Path(cfg["models_dir"]).mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        raise RuntimeError(f"no se pudo crear la carpeta de modelos {cfg['models_dir']}: {e}")
                config.save(cfg)
            self.emit(2, "ok", str(config.path()))

            # 3 · programa
            step = 3
            self.emit(3, "run")
            pyz = None if self.dry else installer.install_program()
            self.emit(3, "ok", str(pyz) if pyz else "ejecutándose desde el código fuente")

            # 4 · servicio
            step = 4
            svc = None
            if n["autostart"]:
                self.emit(4, "run")
                cmd, env = installer.entry_cmd(pyz)
                if os.environ.get("DLLM_HOME"):
                    env = dict(env, DLLM_HOME=os.environ["DLLM_HOME"])
                svc = installer.install_service(cmd, env, dry_run=self.dry)
                self.emit(4, "ok" if svc["ok"] else "warn",
                          svc["path"] or svc["note"] if svc["ok"] else f"{svc['note']}. Arranque manual: {svc['manual']}")
            else:
                self.emit(4, "ok", "omitido (arranca con `dllm run`)")
            shortcuts = []
            if role != "node" and pyz and not self.dry:
                try:  # acceso directo «DLLM» que abre el panel (y arranca DLLM si estaba parado)
                    shortcuts = installer.install_shortcuts(pyz)
                except Exception as e:
                    self.emit(4, "warn", f"no se pudo crear el acceso directo ({e})", label="Acceso directo")
                if shortcuts:
                    self.emit(4, "ok", " · ".join(shortcuts), label="Acceso directo")

            # cortafuegos (Windows: una confirmación de administrador; Linux/macOS: comandos a mano)
            fw = {"ok": False, "auto": False}
            if n["firewall"] and installer.firewall_plan(cfg):
                fw = installer.open_firewall(cfg, dry_run=self.dry)
                if fw["auto"]:
                    self.emit(4, "ok" if fw["ok"] else "warn",
                              fw["detail"] if fw["ok"] else f"{fw['detail']} · ábrelo a mano con los comandos de abajo",
                              label="Cortafuegos")

            # 5 · comprobación
            step = 5
            self.emit(5, "run")
            if svc and svc["ok"] and not self.dry:
                ok, d = installer.check_local(cfg, 25)
                self.emit(5, "ok" if ok else "warn", d)
            else:
                self.emit(5, "ok", "arranca el programa con `dllm run` y vuelve a comprobar con `dllm doctor`")

            res = {"role": role, "name": cfg["name"], "autostart": bool(svc and svc["ok"]),
                   "manual_start": (svc or {}).get("manual") or " ".join(installer.entry_cmd(pyz)[0]),
                   "firewall": [] if fw["ok"] else installer.firewall_hints(cfg),
                   "service_note": (svc or {}).get("note", ""), "install_dir": n["install_dir"],
                   "shortcuts": shortcuts, "pairing": role == "node" and not n["join"],
                   "agent_port": a_["port"],
                   "models_dir": cfg["models_dir"] if role != "node" else None}
            if role != "node":
                res["ui_url"] = f"http://127.0.0.1:{o_['port']}"
                res["api_key"] = o_["api_key"]
                if o_["api_key"]:  # para abrir el panel desde otro equipo sin escribir la clave (va en el #, no viaja)
                    host = n["advertise"] or config.lan_ip()
                    res["lan_url"] = f"http://{host}:{o_['port']}/#key={o_['api_key']}"
                res["join_code"] = self.join_code(cfg, n["advertise"])
            return res
        except Exception as e:
            self.emit(step, "err", str(e))
            raise

    @staticmethod
    def join_code(cfg, advertise=None):
        return config.encode_join({"secret": cfg["secret"], "orch": advertise or config.lan_ip(),
                                   "beacon": cfg["orchestrator"]["beacon_port"], "tag": cfg.get("llama_tag"),
                                   "name": cfg["name"]})


# ================================================================ elegir carpeta
_TK_PICK = ("import sys,tkinter as t,tkinter.filedialog as f\n"
            "r=t.Tk();r.withdraw();r.attributes('-topmost',True);r.lift();r.focus_force();r.update()\n"
            "p=f.askdirectory(initialdir=sys.argv[1],title=sys.argv[2],mustexist=False)\n"
            "sys.stdout.write(p or '')")


# Windows: el asistente no es la ventana activa (lo es el navegador), así que un diálogo normal se abre DETRÁS.
# Se crea una ventana propietaria invisible y siempre encima, se muestra y se trae al frente (con la pulsación
# virtual de Alt que permite a Windows cambiar la ventana activa) y el diálogo se abre sobre ella.
_WIN_PICK = ";".join([
    "Add-Type -AssemblyName System.Windows.Forms",
    "[System.Windows.Forms.Application]::EnableVisualStyles()",
    "try{Add-Type -Name W -Namespace L -MemberDefinition '"
    "[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(System.IntPtr h);"
    "[DllImport(\"user32.dll\")] public static extern void keybd_event(byte k,byte s,uint f,System.UIntPtr e);'}catch{}",
    "$w=New-Object System.Windows.Forms.Form",
    "$w.TopMost=$true;$w.ShowInTaskbar=$false;$w.Opacity=0;$w.FormBorderStyle='None'",
    "$w.StartPosition='CenterScreen';$w.Size=New-Object System.Drawing.Size(1,1)",
    "$w.Show();try{[L.W]::keybd_event(0x12,0,0,[UIntPtr]::Zero);[L.W]::keybd_event(0x12,0,2,[UIntPtr]::Zero);"
    "[void][L.W]::SetForegroundWindow($w.Handle)}catch{};$w.Activate()",
    "$d=New-Object System.Windows.Forms.FolderBrowserDialog",
    "$d.Description=$env:DLLM_PICK_TITLE;$d.ShowNewFolderButton=$true;$d.SelectedPath=$env:DLLM_PICK_INIT",
    "$r=$d.ShowDialog($w);$w.Close()",
    "if($r -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}",
])


def native_pick_dir(initial=None, title="Carpeta de modelos de DLLM"):
    """Abre el diálogo de carpetas del sistema (el asistente corre en este mismo equipo).
    Devuelve {'native': True, 'path': ruta | None si se canceló} o {'native': False} si no hay diálogo disponible."""
    init = str(Path(initial).expanduser()) if initial else str(Path.home())
    if not Path(init).is_dir():
        init = str(Path.home())
    cmds = []
    if os.name == "nt":
        cmds.append(["powershell", "-NoProfile", "-STA", "-Command", _WIN_PICK])
    elif sys.platform == "darwin":
        cmds.append(["osascript", "-e", f'POSIX path of (choose folder with prompt "{title}")'])
    elif has_display():
        if shutil.which("zenity"):
            cmds.append(["zenity", "--file-selection", "--directory", f"--title={title}", f"--filename={init}/"])
        if shutil.which("kdialog"):
            cmds.append(["kdialog", "--getexistingdirectory", init, "--title", title])
    if has_display():
        cmds.append([sys.executable, "-c", _TK_PICK, init, title])  # Tk: viene con Python en Windows y macOS
    env = dict(os.environ, DLLM_PICK_INIT=init, DLLM_PICK_TITLE=title)  # rutas por entorno: sin problemas de comillas
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=900, env=env)
        except (OSError, subprocess.TimeoutExpired):
            continue
        out, err = r.stdout.strip(), (r.stderr or "").lower()
        if r.returncode == 0:
            if out and len(out) > 3:
                out = out.rstrip("/\\")  # sin barra final (pero «C:\\» o «/» se quedan como están)
            return {"native": True, "path": out or None}
        if r.returncode == 1 and c[0] != sys.executable and (not err.strip() or "cancel" in err):
            return {"native": True, "path": None}  # el usuario canceló
    return {"native": False}


def browse_dir(path=None):
    """Para el explorador de carpetas del propio asistente: subcarpetas, carpeta superior, unidades y espacio libre."""
    p = Path(path).expanduser() if path else Path.home()
    while not p.is_dir() and p != p.parent:
        p = p.parent
    p = p.resolve()
    dirs, error = [], ""
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            if e.name.startswith((".", "$")) or e.name in ("System Volume Information",):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
            except OSError:
                pass
            if len(dirs) >= 500:
                break
    except OSError as e:
        error = f"no se puede leer: {e.strerror or e}"
    roots = []
    if os.name == "nt":
        import string
        roots = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    try:
        free = shutil.disk_usage(p).free
    except OSError:
        free = None
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs, "roots": roots,
            "home": str(Path.home()), "free": free, "error": error}


def make_dir(path, name):
    name = (name or "").strip()
    if not name or name in (".", "..") or any(c in name for c in '/\\:*?"<>|'):
        raise ValueError("nombre de carpeta no válido")
    d = Path(path).expanduser() / name
    d.mkdir(parents=False, exist_ok=True)
    return browse_dir(str(d))


# ================================================================ terminal
def _ask(q, default=None, validate=None):
    while True:
        s = input(f"{q}{f' [{default}]' if default is not None else ''}: ").strip()
        s = s or (default if default is not None else "")
        if validate:
            try:
                return validate(s)
            except ValueError as e:
                print(f"  ✗ {e}")
        elif s or default is not None:
            return s


def _yn(q, default=True):
    s = input(f"{q} [{'S/n' if default else 's/N'}]: ").strip().lower()
    return default if not s else s in ("s", "si", "sí", "y", "yes")


def _gib(b):
    return f"{(b or 0) / hw.GiB:.1f} GiB"


def _print_event(ev):
    if ev["status"] == "run" and not ev["detail"]:
        return
    mark = {"run": "…", "ok": "✓", "warn": "!", "err": "✗"}[ev["status"]]
    print(f"  [{ev['step'] + 1}/{ev['total']}] {mark} {ev['label']}: {ev['detail']}".rstrip(), flush=True)


def _choose_role(existing=False):
    print("\nPaso 1/4 · ¿Qué quieres hacer con este equipo?\n  1) Orquestador  (reparte el modelo y ofrece la API)"
          "\n  2) Nodo         (presta su CPU/GPU a un orquestador)\n  3) Ambos"
          + ("\n  4) Desinstalar DLLM de este equipo" if existing else ""))
    opts = ("1", "2", "3", "4") if existing else ("1", "2", "3")

    def v(s):
        if s not in opts:
            raise ValueError("elige " + ", ".join(opts))
        return "uninstall" if s == "4" else ROLES[int(s) - 1]

    return _ask("Elige " + ", ".join(opts), "2", v)


def local_pairing(agent_port, decide=None):
    """Estado del emparejamiento del agente de este equipo (o decide {id, accept}). None si no responde."""
    import http.client
    try:
        c = http.client.HTTPConnection("127.0.0.1", agent_port, timeout=4)
        c.request("POST" if decide else "GET", "/pair/local", body=json.dumps(decide) if decide else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        c.close()
    except (OSError, ValueError):
        return None
    if r.status == 404 and "emparejado" in str(data.get("error", "")):
        return {"state": "completada"}
    if r.status != 200:
        raise RuntimeError(data.get("error") or f"HTTP {r.status}")
    return data


def pair_console(agent_port, ask=input, out=print, poll=2.0):
    """`dllm pair`: espera la solicitud del orquestador, muestra el código de verificación y pide confirmarlo."""
    out("Esperando a que el orquestador reclame este equipo (panel → Nodos → Equipos esperando unirse)…  Ctrl+C sale")
    shown = None
    try:
        while True:
            v = local_pairing(agent_port)
            if v is None:
                out("El agente de este equipo no responde: ¿está DLLM en marcha? (dllm open / dllm run)")
                return 1
            if v["state"] == "completada":
                out("✓ Equipo unido al clúster. Ya puedes cerrar esta ventana.")
                return 0
            if v["state"] == "pendiente" and v.get("id") != shown:
                shown = v["id"]
                out(f"\nEl orquestador «{v['orch']}» ({v['ip']}) quiere usar este equipo.")
                out(f"Código de verificación:  {v['sas']}")
                ok = ask("¿Es el mismo código que muestra su panel? [s/N] ").strip().lower() in ("s", "si", "sí", "y")
                local_pairing(agent_port, {"id": v["id"], "accept": ok})
                out("Aceptado: terminando de unir…" if ok else "Rechazado. Sigue esperando otra solicitud…")
            time.sleep(poll)
    except KeyboardInterrupt:
        return 1


def _print_result(r):
    print("\n=== Listo ===")
    if r["role"] != "node":
        print(f"Panel y API:  {r['ui_url']}   (API OpenAI-compatible en {r['ui_url']}/v1)")
        if r.get("api_key"):
            print(f"API key:      {r['api_key']}")
        print("\nPara añadir nodos, ejecuta el asistente en cada uno y pega este código de unión.\n"
              "Es una CONTRASEÑA del clúster: no lo compartas fuera de tu red.\n")
        print(r["join_code"])
    elif r.get("pairing"):
        print("Este nodo aún no pertenece a ningún clúster. En el panel del orquestador: Nodos → Equipos esperando\n"
              "unirse → Reclamar. Después confirma aquí el código de verificación.")
    else:
        print("Este nodo se anunciará solo al orquestador; deberías verlo en su panel en unos segundos.")
    if not r["autostart"]:
        print(f"\nArranque manual: {r['manual_start']}")
    if r.get("service_note"):
        print(f"Nota: {r['service_note']}")
    if r["firewall"]:
        print("\nSi tienes cortafuegos activo, abre solo lo necesario:")
        for c in r["firewall"]:
            print("  " + c)


def _ask_contribution(o, d):
    p, r = d["platform"], d["recommended"]
    print(f"\n¿Cuánto comparte este equipo? (tiene {_gib(p['ram_total'])} de RAM y {p['cores']} hilos)\n"
          f"  1) Automático: lo gestiona el orquestador y se puede cambiar desde su panel web (recomendado:\n"
          f"     ~{r['mem_gb']} GiB y {r['threads']} hilos)\n"
          f"  2) Limitar desde este equipo (el orquestador no podrá cambiarlo)")
    if _ask("Elige 1 o 2", "1") != "2":
        o["limits_mode"] = "auto"
        return
    o["limits_mode"] = "manual"
    o["share"] = _ask("Porcentaje del equipo a compartir (1-100)", "50")
    print("  Opcional: valores exactos (tienen prioridad sobre el porcentaje). Deja vacío para usar el %.")
    o["mem_gb"] = _ask("  Memoria en GiB", "") or None
    o["threads"] = _ask("  Hilos de CPU", "") or None


def run_tui(a):
    sp = Setup(emit=_print_event, dry_run=a.dry_run)
    d = sp.detect()
    p = d["platform"]
    print(f"\n=== DLLM {__version__} · asistente de instalación ===")
    print(legal_notice())
    gpu = ", ".join(g["name"] for g in p["nvidia"]) or ", ".join(p["other_gpu"]) or "sin GPU detectada"
    print(f"Equipo: {d['hostname']} · {p['os']}/{p['arch']} · {_gib(p['ram_total'])} RAM · {gpu}")
    if d["existing"]:
        print(f"Aviso: ya hay una configuración (rol {d['existing']['role']}); se sustituirá.")
    o = {"role": a.role, "join_code": a.join, "models_dir": a.models_dir, "backend": a.backend,
         "install_dir": a.install_dir, "firewall": not a.no_firewall,
         "bin_dir": a.bin_dir, "name": a.name, "expose": a.expose, "autostart": not a.no_service,
         "agent_port": a.agent_port, "rpc_port": a.rpc_port, "api_port": a.api_port,
         "beacon_port": a.beacon_port, "mem_gb": a.mem_gb, "bw_gbps": a.bw_gbps,
         "advertise_host": a.advertise_host, "share": a.share, "threads": a.threads,
         "limits_mode": a.limits_mode}
    o["role"] = o["role"] or _choose_role(bool(d["existing"]))
    if o["role"] == "uninstall":
        purge = _yn("¿Borrar también llama.cpp y la configuración?", True)
        models = purge and d["existing"]["role"] != "node" and _yn("¿Borrar también los modelos descargados?", False)
        for line in installer.uninstall_all(purge=purge, models=models):
            print("·", line)
        return
    if o["role"] == "node":
        if not o["join_code"]:
            print("\nPaso 2/4 · Pega el código de unión que te dio el orquestador (empieza por DLLM1-).\n"
                  "         Déjalo vacío para que el orquestador reclame este equipo desde su panel.")
            o["join_code"] = _ask("Código (opcional)", "",
                                  validate=lambda s: (config.decode_join(s), s)[1] if s.strip() else s)
    if not o["install_dir"] and not a.yes:
        print("\nCarpeta de DLLM: aquí se guardan llama.cpp" + ("" if o["role"] == "node" else " y los modelos (models)"))
        o["install_dir"] = _ask("Carpeta", d["default_install_dir"])
    if o["role"] != "node":
        if not a.yes and not o["expose"]:
            o["expose"] = _yn("¿Permitir usar la API desde otros equipos de la red? (se creará una API key)", False)
    o["backend"] = o["backend"] or d["suggested_backend"]
    if o["role"] in ("node", "both") and not a.yes and not o["limits_mode"] \
            and all(o[k] is None for k in ("share", "mem_gb", "threads")):
        _ask_contribution(o, d)
    print(f"\nPaso 3/4 · Resumen\n  rol: {o['role']} · llama.cpp: {o['backend']} "
          f"· arranque automático: {'sí' if o['autostart'] else 'no'}")
    if not a.yes:
        if _yn("¿Cambiar opciones avanzadas (nombre, backend)?", False):
            o["name"] = _ask("Nombre del equipo", d["hostname"])
            o["backend"] = _ask(f"Backend {d['backends']}", o["backend"])
        o["autostart"] = _yn("¿Arrancar automáticamente al iniciar sesión?", o["autostart"])
        if not _yn("¿Instalar ahora?", True):
            print("Cancelado.")
            return 1
    print("\nPaso 4/4 · Instalando")
    try:
        r = sp.install(o)
    except Exception as e:
        print(f"\n✗ No se pudo completar: {e}")
        return 1
    _print_result(r)
    if r.get("pairing") and not a.dry_run:
        return pair_console(r["agent_port"])
    return 0


# ================================================================ navegador
class _WebState:
    def __init__(self, token, port, dry):
        self.token, self.port, self.setup, self.last = token, port, Setup(dry_run=dry), time.time()
        self.presence = presence.Presence()


def _orch_nodes():
    cfg = config.load()
    if not cfg or cfg["role"] == "node":
        return {"nodes": []}
    o = cfg["orchestrator"]
    req = urllib.request.Request(f"http://127.0.0.1:{o['port']}/api/state")
    if o.get("api_key"):
        req.add_header("Authorization", f"Bearer {o['api_key']}")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            st = json.load(r)
        return {"state": st["state"], "nodes": [{"name": n["name"], "host": n["host"], "up": n["up"],
                                                 "backend": n["backend"], "cap_bytes": n["cap_bytes"]}
                                                for n in st["nodes"]]}
    except Exception as e:
        return {"nodes": [], "error": str(e)[:120]}


def _make_web_handler(st):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, raw=None, ctype="application/json"):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _guard(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):  # anti DNS-rebinding
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc not in (f"127.0.0.1:{st.port}", f"localhost:{st.port}"):
                return False
            q = parse_qs(urlparse(self.path).query)
            tok = self.headers.get("X-Setup-Token") or (q.get("t") or [""])[0]
            return secrets.compare_digest(tok, st.token)

        def _route(self):
            if not self._guard():
                self.close_connection = True
                return self._send(403, {"error": "acceso denegado"})
            st.last = time.time()
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            sp = st.setup
            try:
                if path == "/":
                    html = pkgutil.get_data("dllm", "setup.html").decode().replace("__TOKEN__", st.token)
                    return self._send(200, raw=html.encode(), ctype="text/html; charset=utf-8")
                if path == "/api/detect":
                    return self._send(200, sp.detect())
                if path == "/api/install" and self.command == "POST":
                    sp.normalize(body)  # valida antes de arrancar para dar el error en el formulario
                    sp.start(body)
                    return self._send(202, {"started": True})
                if path == "/api/progress":
                    since = int((parse_qs(urlparse(self.path).query).get("since") or ["0"])[0])
                    return self._send(200, {"events": sp.events[since:], "done": sp.done,
                                            "error": sp.error, "result": sp.result})
                if path == "/api/nodes":
                    return self._send(200, _orch_nodes())
                if path == "/api/pickdir" and self.command == "POST":
                    return self._send(200, native_pick_dir(body.get("initial")))
                if path == "/api/browse":
                    q = parse_qs(urlparse(self.path).query)
                    return self._send(200, browse_dir((q.get("path") or [None])[0]))
                if path == "/api/mkdir" and self.command == "POST":
                    return self._send(200, make_dir(body.get("path"), body.get("name")))
                if path == "/api/uninstall" and self.command == "POST":
                    if sp.running:
                        raise RuntimeError("hay una instalación en curso")
                    return self._send(200, {"done": installer.uninstall_all(
                        purge=bool(body.get("purge", True)), models=bool(body.get("models")), dry_run=sp.dry)})
                if path == "/api/pairing":
                    cfg = config.load() or {}
                    port = (cfg.get("agent") or {}).get("port", 8765)
                    v = local_pairing(port, {"id": body.get("id"), "accept": bool(body.get("accept"))}
                                      if self.command == "POST" else None)
                    return self._send(200, v or {"state": "sin agente"})
                if path in ("/api/ui/ping", "/api/ui/bye") and self.command == "POST":
                    cid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                    (st.presence.touch if path.endswith("ping") else st.presence.bye)(cid)
                    return self._send(200, {"ok": True})
                if path == "/api/quit" and self.command == "POST":
                    self._send(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                self._send(404, {"error": "no encontrado"})
            except (ValueError, RuntimeError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": str(e)})

        do_GET = do_POST = _route

    return H


def has_display():
    return sys.platform in ("win32", "darwin") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def run_web(a):
    token = secrets.token_urlsafe(16)
    srv = FastHTTPServer(("127.0.0.1", a.port or 0), BaseHTTPRequestHandler)
    port = srv.server_address[1]
    st = _WebState(token, port, a.dry_run)
    srv.RequestHandlerClass = _make_web_handler(st)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/?t={token}"
    closed = threading.Event()
    print(f"Asistente de DLLM en: {url}\n(solo accesible desde este equipo; ciérralo desde la propia página o con Ctrl+C)", flush=True)
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def idle_watch():  # se cierra solo tras 45 min sin uso
        while time.time() - st.last < 45 * 60:
            time.sleep(30)
        srv.shutdown()

    threading.Thread(target=idle_watch, daemon=True).start()
    if not getattr(a, "hide_console", False) or a.no_browser:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    # consola escondida mientras el asistente está abierto; reaparece si se cierra el navegador sin terminar
    def serve():
        try:
            srv.serve_forever()
        finally:
            closed.set()

    threading.Thread(target=serve, daemon=True).start()
    presence.hide_console()
    res = presence.watch(lambda: None if closed.is_set() else st.presence.view(), lambda: webbrowser.open(url),
                         "el asistente", running="el asistente de instalación")
    if res == "salir":
        srv.shutdown()
    return 0


def main(a):
    mode = "web" if a.gui else "tui" if a.tui else ("web" if has_display() and sys.stdin.isatty() else "tui")
    if mode == "web":
        return run_web(a)
    return run_tui(a)
