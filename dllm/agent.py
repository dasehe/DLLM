# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Agente de nodo: lanza rpc-server (solo en loopback) y lo expone mediante un proxy autenticado.

Superficie de red del agente:
  - HTTP  (--port, 8765):     API de control, TODAS las peticiones firmadas con HMAC.
  - TCP   (--rpc-port, 50051): proxy hacia rpc-server; exige handshake HMAC mutuo.
  - rpc-server escucha SOLO en 127.0.0.1 (puerto efímero): nadie lo alcanza sin pasar por el proxy.
"""
import atexit
import collections
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import __version__, auth, config, devices, hw, limits, pairing, telemetry
from .util import FastHTTPServer, log, pipe, tune

MAX_BODY = 64 << 20


KEY_LINES = re.compile(r"assert|error|abort|fail|out of memory|oom|exception|signal|device|lost|vk|vulkan", re.I)


def exit_record(slot, code, tail, why=None):
    """Motivo de la muerte de un rpc-server: código, explicación y las líneas más relevantes de su salida."""
    lines = [l for l in tail if l.strip()]
    key = [l for l in lines if KEY_LINES.search(l)]
    return {"t": time.time(), "slot": slot, "code": code, "why": why,
            "lines": [l[:300] for l in (key or lines)[-6:]]}


class Rpc:
    """Un rpc-server del agente (uno por slot de modelo)."""

    def __init__(self, slot):
        self.slot, self.proc, self.port, self.want = slot, None, None, False
        self.tail = collections.deque(maxlen=40)
        self.last_exit = None  # cómo murió la última vez (ver exit_record)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


class Agent:
    def __init__(self, a):
        self.a = a
        self.secret = a.secret
        self.name = a.name or socket.gethostname()
        self.t0 = time.time()
        self.probe = hw.Probe(a.backend, a.reserve_gb, a.bw_gbps, a.gpu_index)
        # modo de contribución: 'auto' (lo ajusta el orquestador) o 'manual' (fijado aquí, solo lectura fuera)
        self.mode = getattr(a, "limits_mode", None) or "auto"
        self.limits = {"share": getattr(a, "share", None), "mem_gb": a.mem_gb, "threads": a.threads}
        self.config_file = getattr(a, "config_file", None)
        self.rpc_bin = a.rpc_bin or hw.find_bin("rpc-server", a.bin_dir)
        self.build = hw.build_id(self.rpc_bin) if self.rpc_bin else None
        # actualización de llama.cpp ordenada por el orquestador (misma build en todo el clúster)
        self.auto_update = not getattr(a, "no_auto_update", False)
        self.llama_backend = getattr(a, "llama_backend", None)
        self.update = {"state": "inactiva"}
        self.devices, self.device, self.device_error = [], None, ""
        self.allow = set(a.allow_ip or [])
        self.announce = list(a.announce_to or [])
        self.beacon_port = a.beacon_port
        # sin secreto: espera a que un orquestador lo reclame (ver pairing.py)
        self.pairing = None if self.secret else pairing.NodePairing(self.adopt)
        # si solo se acepta al propio equipo (rol «ambos»), ni siquiera se escucha en la red
        self.listen = "127.0.0.1" if self.allow and self.allow <= {"127.0.0.1", "::1", "localhost"} else "0.0.0.0"
        self.lock = threading.Lock()
        self.rpcs = {}  # slot → Rpc (un rpc-server por modelo cargado; el 0 es el de siempre)
        self.restarts = 0
        self.detect_devices()
        self.sampler = telemetry.Sampler(lambda: self.device)

    # ------------------------------------------------------------ dispositivos
    def detect_devices(self):
        """Pregunta a rpc-server qué aceleradores ve y elige uno (el indicado con --device o el de más memoria)."""
        if not self.rpc_bin:
            return
        try:
            self.devices = devices.probe_rpc_server(self.rpc_bin)
            forced_cpu = self.a.backend == "cpu"
            dev = None if forced_cpu else devices.choose(self.devices, getattr(self.a, "device", None))
            self.device, self.device_error = dev, ""
            self.probe.set_device(dev)
            if dev:
                log(f"dispositivo: {dev['name']} · {dev['desc']} · {dev['total'] / hw.GiB:.1f} GiB "
                    f"({dev['free'] / hw.GiB:.1f} libres)")
            elif self.devices:
                log("sin acelerador utilizable: se presta CPU y RAM")
        except (RuntimeError, ValueError) as e:
            self.device_error = str(e)[:300]
            log(f"detección de dispositivos: {e}")

    def device_refresh_loop(self):
        """La memoria libre de la GPU cambia (otros programas): se vuelve a medir mientras rpc-server no la usa."""
        while True:
            time.sleep(60)
            if self.device and not self.rpc_running() and self.update.get("state") != "descargando":
                try:
                    for d in devices.probe_rpc_server(self.rpc_bin):
                        if d["name"] == self.device["name"]:
                            self.device.update(free=d["free"], total=d["total"])
                except RuntimeError:
                    pass

    # ------------------------------------------------------------ actualización de llama.cpp
    def install_llama(self, tag):
        if not self.auto_update:
            raise PermissionError("este nodo tiene desactivada la actualización automática de llama.cpp")
        if not re.fullmatch(r"b\d{1,7}", tag or ""):
            raise ValueError("etiqueta de llama.cpp no válida (formato bNNNN)")
        with self.lock:
            if self.update.get("state") == "descargando":
                return self.update
            self.update = {"state": "descargando", "tag": tag, "progress": 0, "detail": ""}
        threading.Thread(target=self._install_worker, args=(tag,), daemon=True).start()
        return self.update

    def _install_worker(self, tag):
        from . import installer
        try:
            info = installer.platform_info()
            backend = self.llama_backend or installer.suggest_backends(info)[0]

            def progress(pct, msg):
                self.update.update(progress=pct, detail=msg)

            log(f"actualizando llama.cpp a {tag} ({backend}) por orden del orquestador")
            res = installer.install_llama(config.llama_dir({"install_dir": self.a.install_dir}), tag, backend, info, progress, need=("rpc-server",))
            with self.lock:
                self._kill_all()
                self.rpc_bin = hw.find_bin("rpc-server", res["bin_dir"])
                self.build = res["build"]
                self.a.bin_dir = res["bin_dir"]
            self.detect_devices()
            if self.config_file:
                cfg = config.load()
                if cfg:
                    cfg["bin_dir"], cfg["llama_tag"], cfg["llama_backend"] = res["bin_dir"], res["tag"], backend
                    config.save(cfg)
            self.update = {"state": "completada", "tag": res["tag"], "build": res["build"], "progress": 100}
            log(f"llama.cpp actualizado: {res['build']}")
        except Exception as e:
            self.update = {"state": "error", "tag": tag, "error": str(e)[:300]}
            log(f"no se pudo actualizar llama.cpp: {e}")

    # ------------------------------------------------------------ emparejamiento
    def adopt(self, data):
        """El orquestador lo ha reclamado y se ha confirmado aquí: guarda el secreto y pasa a nodo normal."""
        orch, beacon = data["orch_ip"], int(data.get("beacon_port") or self.beacon_port)
        restrict = getattr(self.a, "restrict_pair", True)
        with self.lock:
            self.announce = [f"{orch}:{beacon}"]
            if restrict:
                self.allow = {orch, "127.0.0.1"}
            self.beacon_port = beacon
            self.secret = data["secret"]
            self.pairing = None
        if self.config_file:
            try:
                cfg = config.load() or {}
                cfg["secret"] = data["secret"]
                cfg.setdefault("orchestrator", {})["beacon_port"] = beacon
                cfg.setdefault("agent", {})["announce_to"] = list(self.announce)
                if restrict:
                    cfg["agent"]["allow_ip"] = sorted(self.allow)
                config.save(cfg)
            except Exception as e:
                log("no se pudo guardar la configuración tras el emparejamiento:", e)
        log(f"emparejado con el orquestador {data.get('orch_name') or orch} ({orch})")

    # ------------------------------------------------------------ rpc-server
    def ip_ok(self, ip):
        return not self.allow or ip in self.allow

    def rpc_running(self, slot=None):
        """¿Hay rpc-server vivo? slot=None: en cualquier slot."""
        rs = self.rpcs.values() if slot is None else [self.rpcs.get(slot)]
        return any(r is not None and r.alive() for r in rs)

    def _spawn(self, r):
        r.port = hw.free_port()  # puerto nuevo cada vez: evita choques con sockets en TIME_WAIT
        cmd = [self.rpc_bin, "-H", "127.0.0.1", "-p", str(r.port)]
        cmd += ["-t", str(self.probe.snapshot(self.limits)["threads"])]  # límite real de CPU
        if self.device:
            cmd += ["-d", self.device["name"]]  # solo la GPU elegida (por defecto rpc-server las expone todas)
        elif self.a.backend == "cpu" and any(devices.is_cpu(d) for d in self.devices):
            cmd += ["-d", next(d["name"] for d in self.devices if devices.is_cpu(d))]
        if self.a.cache:
            cmd += ["-c"]
        cmd += self.a.rpc_arg or []
        r.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ),
                                  text=True, bufsize=1)
        threading.Thread(target=self._read, args=(r, r.proc), daemon=True).start()
        log(f"rpc-server (slot {r.slot}) lanzado:", " ".join(cmd))

    @staticmethod
    def _read(r, proc):
        for line in proc.stdout:
            r.tail.append(line.rstrip())

    @staticmethod
    def _kill_one(r):
        p, r.proc = r.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def _kill_all(self):
        for r in self.rpcs.values():
            r.want = False
            self._kill_one(r)

    def start_rpc(self, slot=0):
        if not self.rpc_bin:
            raise RuntimeError("rpc-server no encontrado (usa --bin-dir o --rpc-bin)")
        if not (isinstance(slot, int) and 0 <= slot < auth.MAX_SLOTS):
            raise ValueError(f"slot no válido (0-{auth.MAX_SLOTS - 1})")
        with self.lock:
            r = self.rpcs.setdefault(slot, Rpc(slot))
            self._kill_one(r)
            r.want = True
            self._spawn(r)
        deadline = time.time() + 20
        while time.time() < deadline:
            if r.proc.poll() is not None:
                from .installer import explain_exit
                why = explain_exit(r.proc.returncode) or " | ".join(list(r.tail)[-5:])
                raise RuntimeError(f"rpc-server terminó (código {r.proc.returncode}): {why}")
            try:
                # sondeo: conectar y cerrar. rpc-server acepta clientes en bucle, así que no le afecta.
                socket.create_connection(("127.0.0.1", r.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("rpc-server no abrió su puerto en 20 s")

    def stop_rpc(self, slot=0):
        """slot=None: para todos."""
        with self.lock:
            for k, r in list(self.rpcs.items()):
                if slot is None or k == slot:
                    r.want = False
                    self._kill_one(r)
                    if k != 0:
                        del self.rpcs[k]

    def watchdog(self):
        """Si un rpc-server muere estando activo, lo relanza."""
        while True:
            time.sleep(1)
            for r in list(self.rpcs.values()):
                if r.want and not r.alive():
                    self.restarts += 1
                    from .installer import explain_exit
                    code = r.proc.returncode if r.proc else None
                    r.last_exit = exit_record(r.slot, code, list(r.tail), explain_exit(code))
                    log(f"rpc-server (slot {r.slot}) caído, código {code} (reinicio #{self.restarts}): "
                        + " | ".join(r.last_exit["lines"][-3:]))
                    try:
                        with self.lock:
                            if r.want:
                                self._spawn(r)
                    except Exception as e:
                        log("no se pudo relanzar:", e)
                        time.sleep(5)

    # ------------------------------------------------------------ proxy TCP autenticado
    def serve_proxy(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.listen, self.a.rpc_port))
        srv.listen(16)
        while True:
            c, addr = srv.accept()
            threading.Thread(target=self._proxy_conn, args=(c, addr), daemon=True).start()

    def _proxy_conn(self, c, addr):
        try:
            if not self.ip_ok(addr[0]):
                raise PermissionError("IP no permitida")
            if not self.secret:
                raise PermissionError("nodo sin emparejar")
            tune(c)
            c.settimeout(5)
            mode = auth.server_handshake(c, self.secret)
            slot = 0
            if mode.startswith("rpc:"):
                mode, slot = "rpc", int(mode[4:])
            if mode == "probe":  # sonda de enlace del orquestador (RTT/ancho de banda); no toca rpc-server
                c.settimeout(20)
                try:
                    auth.serve_probe(c)
                finally:
                    c.close()
                return
            c.settimeout(None)
            rp = self.rpcs.get(slot)
            if rp is None or not rp.alive():
                raise ConnectionError(f"rpc-server (slot {slot}) no está activo")
            r = socket.create_connection(("127.0.0.1", rp.port), timeout=5)
            r.settimeout(None)
            tune(r)
        except Exception as e:
            log(f"proxy RPC: conexión de {addr[0]} rechazada ({e})")
            c.close()
            return
        pipe(c, r, self.sampler.add_bytes)

    # ------------------------------------------------------------ descubrimiento
    def beacon_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            pr = self.pairing
            if pr:  # sin emparejar: anuncio sin firma (solo nombre y puerto) para que el orquestador lo liste
                msg = pairing.beacon(self.name, self.a.port, pr.pid)
                targets = []
                for t in (self.announce or ["255.255.255.255"]):
                    h, _, p = t.partition(":")
                    targets.append((h, int(p) if p else self.beacon_port))
            else:
                targets = []
                for t in (self.announce or ["255.255.255.255"]):
                    h, _, p = t.partition(":")
                    targets.append((h, int(p) if p else self.beacon_port))
                msg = auth.sign_beacon(self.secret, {"name": self.name, "http_port": self.a.port,
                                                     "rpc_port": self.a.rpc_port, "ts": time.time(),
                                                     "v": __version__})
            for t in targets:
                try:
                    s.sendto(msg, t)
                except OSError:
                    pass
            time.sleep(self.a.beacon_interval)

    # ------------------------------------------------------------ límites
    def set_limits(self, body):
        """Cambio pedido por el orquestador. Solo en modo auto; persiste si el agente corre con config.json."""
        if self.mode != "auto":
            raise PermissionError("este nodo fija sus límites localmente (modo manual); cámbialos en el propio "
                                  "nodo con el asistente")
        dev_changed = "device" in body
        if dev_changed:  # "" = automático (el de más memoria); si no, nombre o parte de la descripción
            want = (body.get("device") or "").strip() or None
            if want and want.upper() != "CPU":
                devices.choose(self.devices, want)  # ValueError con la lista si no existe
        new = limits.validate(body, self.probe.hardware())
        self.limits = new
        if dev_changed:
            self.a.device = want
            self.a.backend = "cpu" if want and want.upper() == "CPU" else ("auto" if self.a.backend == "cpu" else self.a.backend)
            self.detect_devices()
        if self.config_file:
            cfg = config.load()
            if cfg:
                cfg["agent"].update(new)
                if dev_changed:
                    cfg["agent"]["device"] = want
                    cfg["agent"]["backend"] = self.a.backend
                config.save(cfg)
        log(f"límites actualizados por el orquestador: {new}")
        s = self.info()
        s["applies"] = "en la próxima carga del modelo" if self.rpc_running() else "inmediatamente"
        return s

    # ------------------------------------------------------------ info
    def last_rpc_exit(self):
        exits = [r.last_exit for r in self.rpcs.values() if r.last_exit]
        return max(exits, key=lambda e: e["t"]) if exits else None

    def info(self):
        s = self.probe.snapshot(self.limits)
        s.update({"limits_mode": self.mode, "limits": self.limits})
        s.update({"name": self.name, "version": __version__, "rpc_bin": bool(self.rpc_bin),
                  "rpc_build": self.build, "rpc_running": self.rpc_running(), "rpc_port": self.a.rpc_port,
                  "rpc_restarts": self.restarts, "rpc_last_exit": self.last_rpc_exit(), "rpc_slots": sorted(k for k, r in self.rpcs.items() if r.alive()), "uptime": int(time.time() - self.t0),
                  "devices": self.devices, "device": self.device and self.device["name"],
                  "device_desc": self.device and self.device["desc"], "device_error": self.device_error,
                  "auto_update": self.auto_update, "llama_backend": self.llama_backend, "update": self.update,
                  "telemetry": self.sampler.latest})
        return s


def make_handler(agent):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dllm-agent"

        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, raw=None, ctype="application/json"):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _reject(self, code, msg):
            self.close_connection = True  # no leemos el cuerpo: cerrar evita desincronizar keep-alive
            self._send(code, {"error": msg})
            log(f"HTTP {code} para {self.client_address[0]} {self.command} {self.path}: {msg}")

        def _auth(self):
            """Verifica la firma ANTES de leer el cuerpo; devuelve el cuerpo o None."""
            if not agent.ip_ok(self.client_address[0]):
                self._reject(403, "ip no permitida")
                return None
            bh = self.headers.get("X-DLLM-Body", auth.EMPTY_HASH)
            if not auth.verify_request(agent.secret, self.command, self.path, bh,
                                       self.headers.get("X-DLLM-Time"), self.headers.get("X-DLLM-Sig")):
                self._reject(401, "firma inválida")
                return None
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._reject(413, "cuerpo demasiado grande")
                return None
            body = self.rfile.read(n) if n else b""
            if auth.body_hash(body) != bh:
                self._reject(401, "hash de cuerpo no coincide")
                return None
            return body

        def _pair_route(self, p):
            """Emparejamiento: solo mientras el nodo no tiene secreto. /pair/local*: solo desde este equipo."""
            pr = agent.pairing
            if not pr:
                return self._send(404, {"error": "este nodo ya está emparejado"})
            ip = self.client_address[0]
            n = int(self.headers.get("Content-Length") or 0)
            if n > 16384:
                return self._reject(413, "cuerpo demasiado grande")
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                local = ip in ("127.0.0.1", "::1")
                if p.path == "/pair/request" and self.command == "POST":
                    return self._send(200, pr.request(ip, body))
                if p.path == "/pair/status":
                    return self._send(200, pr.status((parse_qs(p.query).get("id") or [""])[0]))
                if p.path == "/pair/complete" and self.command == "POST":
                    return self._send(200, pr.complete(ip, body))
                if p.path == "/pair/local" and local:
                    if self.command == "POST":
                        return self._send(200, pr.decide(body.get("id"), bool(body.get("accept"))))
                    return self._send(200, dict(pr.local_view(), name=agent.name))
                return self._send(404, {"error": "no encontrado"})
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})

        def _route(self):
            p = urlparse(self.path)
            if p.path.startswith("/pair/"):
                return self._pair_route(p)
            if agent.pairing:
                return self._reject(503, "nodo sin emparejar: reclámalo desde el panel del orquestador")
            body = self._auth()
            if body is None:
                return
            try:
                if p.path == "/ping":
                    self._send(200, {"t": time.time()})
                elif p.path == "/info":
                    self._send(200, agent.info())
                elif p.path == "/rpc/start" and self.command == "POST":
                    try:
                        slot = int((json.loads(body or b"{}")).get("slot", 0))
                        agent.start_rpc(slot)
                    except (ValueError, TypeError, AttributeError) as e:
                        return self._send(400, {"error": str(e)})
                    self._send(200, {"ok": True, "rpc_port": agent.a.rpc_port, "slot": slot})
                elif p.path == "/limits" and self.command == "POST":
                    try:
                        self._send(200, agent.set_limits(json.loads(body or b"{}")))
                    except PermissionError as e:
                        self._send(403, {"error": str(e)})
                    except ValueError as e:
                        self._send(400, {"error": str(e)})
                elif p.path == "/llama/install" and self.command == "POST":
                    try:
                        self._send(202, agent.install_llama(json.loads(body or b"{}").get("tag")))
                    except PermissionError as e:
                        self._send(403, {"error": str(e)})
                    except ValueError as e:
                        self._send(400, {"error": str(e)})
                elif p.path == "/rpc/stop" and self.command == "POST":
                    try:
                        sl = (json.loads(body or b"{}")).get("slot", 0)
                        agent.stop_rpc(None if sl == "all" else int(sl))
                    except (ValueError, TypeError, AttributeError) as e:
                        return self._send(400, {"error": str(e)})
                    self._send(200, {"ok": True})
                elif p.path == "/bench/up" and self.command == "POST":
                    self._send(200, {"bytes": len(body)})
                elif p.path == "/bench/down":
                    n = min(int(parse_qs(p.query).get("n", ["0"])[0]), MAX_BODY)
                    self._send(200, raw=bytes(n), ctype="application/octet-stream")
                else:
                    self._send(404, {"error": "no encontrado"})
            except Exception as e:
                self._send(500, {"error": str(e)})

        do_GET = do_POST = _route

    return H


def run(a):
    if not a.secret and not getattr(a, "pairing", False):
        sys.exit("falta el secreto del clúster: --secret, DLLM_SECRET o `dllm init` en este equipo "
                 "(o --pairing para que lo reclame un orquestador)")
    ag = Agent(a)
    snap = ag.probe.snapshot(ag.limits)
    log(f"agente '{ag.name}' · backend={snap['backend']} · aporta {snap['cap_bytes'] / hw.GiB:.1f} GiB "
        f"({snap['mem_source']}) y {snap['threads']}/{snap['cores']} hilos ({snap['threads_source']}) · modo "
        f"{ag.mode} · bw≈{snap['bw_gbps']} GB/s · http={a.port} rpc={a.rpc_port}")
    log(f"rpc-server: {ag.rpc_bin or 'NO ENCONTRADO'} · build: {ag.build}")
    if a.allow_ip:
        log("solo se aceptan conexiones de:", ", ".join(a.allow_ip))
    if ag.pairing:
        log("sin emparejar: esperando a que un orquestador lo reclame (confírmalo con `dllm pair`)")
    atexit.register(ag.stop_rpc)  # que no quede rpc-server huérfano al salir
    # primero el puerto HTTP: si lo ocupa otro proceso (p. ej. un agente de una instalación anterior que sigue vivo)
    # no se anuncia nada, o el orquestador reclamaría a ese otro proceso y vería «ya está emparejado»
    try:
        srv = FastHTTPServer((ag.listen, a.port), make_handler(ag))
    except OSError as e:
        sys.exit(f"no se puede abrir el puerto {a.port} del agente ({e}): lo usa otro proceso, probablemente un agente "
                 "de una instalación anterior que sigue en marcha. Ciérralo (Windows: Administrador de tareas → "
                 "pythonw.exe / python.exe; Linux: `ss -ltnp | grep " + str(a.port) + "`) y vuelve a iniciar DLLM")
    srv.daemon_threads = True
    for fn in (ag.serve_proxy, ag.watchdog, ag.beacon_loop, ag.device_refresh_loop, ag.sampler.loop):
        threading.Thread(target=fn, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ag.stop_rpc()
