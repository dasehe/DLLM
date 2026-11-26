# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Orquestador DLLM: descubre agentes, planifica el reparto, lanza llama-server sobre túneles
autenticados hacia los rpc-server de los nodos, expone una API OpenAI-compatible y se
recupera solo si un nodo cae."""
import base64
import collections
import gzip
import hashlib
import hmac
import http.client
import json
import os
import pkgutil
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (__version__, auth, config, devices, gguf, hw, installer, legal_notice, models, pairing, presence,
               scheduler)
from . import backup, discovery, engine, eventlog, i18n
from .ledger import HOST, Ledger
from .util import FastHTTPServer, Tunnel, log, probe_link

CPU_BW_GBPS = 20  # ancho de banda de memoria supuesto al usar la RAM de un nodo con GPU (estimación gruesa)
IDLE, LOADING, READY, RECOVERING, ERROR = "IDLE", "LOADING", "READY", "RECOVERING", "ERROR"
UNLOADING = "UNLOADING"  # descargando / cancelando la carga


class Busy(Exception):
    pass


class Cancelled(Exception):
    pass


class LoadError(Exception):
    pass


class AgentError(RuntimeError):
    """Respuesta de error de un agente, con su código HTTP y su mensaje."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def same_build(a, b):
    """Misma compilación de llama.cpp: se compara el número (b12387), no el texto entre paréntesis."""
    return installer.tag_from_build(a) == installer.tag_from_build(b)


def parse_prometheus(text):
    """{nombre: valor} de un texto Prometheus (sin etiquetas; si hay varias series, se suman)."""
    out = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].split("{", 1)[0]
        try:
            out[name] = out.get(name, 0.0) + float(parts[1])
        except ValueError:
            pass
    return out


# ============================================================ cliente de agentes
def _headers(secret, method, path, data):
    bh = auth.body_hash(data)
    ts, sig = auth.sign_request(secret, method, path, bh)
    return {"X-DLLM-Time": ts, "X-DLLM-Sig": sig, "X-DLLM-Body": bh, "Content-Type": "application/json"}


def call_agent(secret, node, method, path, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else b""
    c = http.client.HTTPConnection(node.host, node.http_port, timeout=timeout)
    try:
        c.request(method, path, body=data or None, headers=_headers(secret, method, path, data))
        r = c.getresponse()
        payload = r.read()
        if r.status != 200:
            try:
                msg = json.loads(payload).get("error") or ""
            except ValueError:
                msg = payload[:200].decode("utf-8", "replace")
            raise AgentError(r.status, f"{node.id} {path}: HTTP {r.status} {msg}")
        return json.loads(payload or b"{}")
    finally:
        c.close()


def is_local_host(host):
    """¿Ese host es este mismo equipo? (loopback, la IP LAN propia o el nombre del equipo)."""
    if host in ("localhost", "::1") or host.startswith("127."):
        return True
    try:
        mine = {config.lan_ip(), socket.gethostbyname(socket.gethostname())}
        return socket.gethostbyname(host) in mine
    except OSError:
        return False


def bench_node(secret, node, size=16 << 20):
    """Enlace orquestador↔nodo. Primero la sonda del túnel autenticado (RTT + bloques de 4 MiB, ver
    util.probe_link); si el agente es anterior a 0.5.0 o no responde, el bench HTTP firmado de siempre.
    Añade `tier` (scheduler.classify) y `local`."""
    local = is_local_host(node.host)
    try:
        r = probe_link(node.host, node.rpc_port, secret)
    except Exception:
        r = _bench_http(secret, node, size)
        r["via"] = "http"
    r["local"] = local
    r["tier"] = scheduler.classify(local, r["rtt_ms"], r["mbps"])
    return r


def _bench_http(secret, node, size):
    """RTT y throughput real orquestador↔nodo (HTTP, 16 MiB en cada sentido)."""
    c = http.client.HTTPConnection(node.host, node.http_port, timeout=60)
    try:
        def req(method, path, data=b""):
            h = _headers(secret, method, path, data)  # el hash se calcula fuera del cronómetro
            t = time.perf_counter()
            c.request(method, path, body=data or None, headers=h)
            r = c.getresponse()
            r.read()
            dt = time.perf_counter() - t
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} en {path}")
            return dt

        rtts = [req("GET", "/ping") for _ in range(6)]
        up = req("POST", "/bench/up", bytes(size))
        down = req("GET", f"/bench/down?n={size}")
        mb = lambda dt: size * 8 / 1e6 / dt
        return {"rtt_ms": round(min(rtts[1:]) * 1000, 2), "up_mbps": round(mb(up)), "down_mbps": round(mb(down)),
                "mbps": round(min(mb(up), mb(down))), "at": time.time()}
    finally:
        c.close()


# ============================================================ registro de nodos
class Node:
    def __init__(self, host, http_port, static=False):
        self.host, self.http_port, self.static = host, http_port, static
        self.id = f"{host}:{http_port}"
        self.info, self.net = {}, None
        self.last_seen, self.last_error = 0.0, ""
        self.hist = collections.deque(maxlen=150)  # telemetría reciente (≈5 min a 2 s por muestra)

    @property
    def name(self):
        return self.info.get("name") or self.id

    @property
    def rpc_port(self):
        return self.info.get("rpc_port")


class Registry:
    def __init__(self, secret, node_timeout=10.0, probe_interval=3.0):
        self.secret, self.node_timeout, self.interval = secret, node_timeout, probe_interval
        self.nodes, self.lock = {}, threading.RLock()
        self.pending = {}  # ip → anuncio de un nodo sin emparejar (ver pairing.py)
        self.banned = {}   # host → instante hasta el que se ignoran sus anuncios (nodo eliminado a mano)

    def add(self, host, http_port, static=False):
        with self.lock:
            nid = f"{host}:{http_port}"
            if nid not in self.nodes:
                self.nodes[nid] = Node(host, http_port, static)
                log(f"nodo nuevo: {nid}")
            return self.nodes[nid]

    def all(self):
        with self.lock:
            return list(self.nodes.values())

    def remove(self, nid, ban_s=0):
        """Olvida un nodo. Con `ban_s` ignora sus anuncios ese tiempo (si sigue vivo, no reaparece al instante)."""
        with self.lock:
            n = self.nodes.pop(nid, None)
            if n:
                self.pending.pop(n.host, None)
                if ban_s:
                    self.banned[n.host] = time.time() + ban_s
            return n

    def get(self, nid):
        with self.lock:
            return self.nodes.get(nid)

    def is_up(self, n):
        return time.time() - n.last_seen < self.node_timeout

    def up_ids(self):
        return frozenset(n.id for n in self.all() if self.is_up(n))

    def beacon_loop(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError as e:
            log(f"no puedo escuchar beacons UDP en {port}: {e} (usa --node para añadir nodos a mano)")
            return
        while True:
            data, addr = s.recvfrom(4096)
            if self.banned.get(addr[0], 0) > time.time():
                continue
            pb = pairing.parse_beacon(data)
            if pb:
                with self.lock:
                    self.pending[addr[0]] = dict(pb, host=addr[0], t=time.time())
                continue
            d = auth.verify_beacon(self.secret, data)
            if d:
                self.add(addr[0], int(d["http_port"]))
                self.pending.pop(addr[0], None)
            else:
                log(f"beacon inválido/firma incorrecta desde {addr[0]} (ignorado)")

    def probe(self, n):
        try:
            n.info = call_agent(self.secret, n, "GET", "/info", timeout=2.5)
            n.last_seen, n.last_error = time.time(), ""
            t = n.info.get("telemetry") or {}
            if t.get("t") and (not n.hist or n.hist[-1]["t"] != t["t"]):
                n.hist.append(t)
        except Exception as e:
            n.last_error = str(e)[:200]

    def probe_loop(self):
        while True:
            ths = [threading.Thread(target=self.probe, args=(n,), daemon=True) for n in self.all()]
            for t in ths:
                t.start()
            for t in ths:
                t.join(timeout=4)
            with self.lock:  # olvida nodos descubiertos por beacon que llevan 10 min mudos
                for nid in [k for k, n in self.nodes.items()
                            if not n.static and time.time() - n.last_seen > 600 and n.last_seen]:
                    del self.nodes[nid]
            time.sleep(self.interval)


# ============================================================ clúster (modelo cargado)
class RouteError(Exception):
    """Error de enrutado/carga bajo demanda. `retry_after` (s) añade la cabecera Retry-After (429/503)."""

    def __init__(self, status, msg, retry_after=None):
        super().__init__(msg)
        self.status, self.retry_after = status, retry_after


def oai_error(status, msg):
    """Cuerpo de error en el formato estándar de OpenAI."""
    typ, code = {400: ("invalid_request_error", "invalid_request"), 429: ("requests", "rate_limit_exceeded"),
                 502: ("server_error", "bad_gateway"), 504: ("server_error", "timeout")}.get(
        status, ("server_error", "service_unavailable"))
    return {"error": {"message": msg, "type": typ, "param": None, "code": code}}


class Cluster:
    """Un SLOT de modelo: un llama-server y, en cada nodo que use, su propio rpc-server y su propio túnel HMAC.
    El slot 0 (el «principal») además hace de coordinador: crea y reúne los demás (`self.slots`) y comparte con
    ellos el registro de nodos, el gestor de modelos y el libro de memoria (`ledger`)."""

    def __init__(self, cfg, reg, slot=0, parent=None, pinned=True):
        self.cfg, self.reg = cfg, reg
        self.slot, self.parent, self.pinned, self.dead = slot, parent, pinned, False
        self.last_used = time.time()
        self.ondemand_lock = parent.ondemand_lock if parent else threading.Lock()
        self.ledger = parent.ledger if parent else Ledger(
            getattr(cfg, "vram_safety_mb", 256) << 20, getattr(cfg, "ram_safety_mb", 1024) << 20)
        self.slots = parent.slots if parent else {0: self}
        self.lock, self.work = threading.RLock(), threading.Lock()
        self.state, self.error, self.model, self.ctx = IDLE, "", None, None
        self.plan, self.proc, self.tunnels, self.used = None, None, [], []
        self.llama_port = None
        self.tools, self.tool_template = False, False  # --jinja activo / la plantilla admite herramientas
        self.direct = None  # dispositivo local en el que corre llama-server sin RPC (None = reparto por RPC)
        self.driver = engine.driver_for({})
        self.inflight = 0
        self.desired = None  # (nombre, ctx) que el usuario quiere tener cargado
        self.gen, self.recoveries, self.last_nodeset = 0, 0, frozenset()
        self.requests, self.req_errors = 0, 0
        self.logbuf = collections.deque(maxlen=200)
        self.last_crash = None
        self.events = parent.events if parent else collections.deque(maxlen=100)
        self.elog = parent.elog if parent else eventlog.EventLog(getattr(cfg, "events_file", None))
        self.metrics_req = eventlog.SlotMetrics()   # peticiones de este modelo (latencias, errores)
        self.targets = None   # [{"node","device"}]: nodos y dispositivo elegidos a mano para este modelo (None = automático)
        self._node_up = {}    # nodo → estaba en línea (para registrar altas y caídas)
        self.llama_build = parent.llama_build if parent else (hw.build_id(cfg.llama_bin) if cfg.llama_bin else None)
        self.models = parent.models if parent else models.Manager(cfg.models_dir)
        self.presence = parent.presence if parent else presence.Presence()  # pestañas del panel (`dllm open`)
        self.claims = parent.claims if parent else {}  # host → emparejamiento en curso
        self.jobs = parent.jobs if parent else {"scan": {}, "bench": {}}  # búsqueda de red y medición en curso
        self._local_devs = None   # dispositivos locales de llama-server (no cambian: se cachean)
        self.llama_hist = collections.deque(maxlen=150)  # tokens/s y peticiones (de /metrics)
        self._sync_tried = {}     # nodo → hora del último intento de sincronizar llama.cpp
        self.mmproj = None

    # ---------------------------------------------------------- utilidades
    def event(self, msg, level=None, node=None, kind=None, detail=None):
        """Registra un evento (panel, `/api/events` y events.jsonl). nivel: info/warn/error (si no se da, se deduce)."""
        msg = f"[slot {self.slot}] {msg}" if self.slot else msg
        log(msg)
        ev = self.elog.add(msg, level, self.slot, node, kind, detail)
        self.events.append({"t": ev["t"], "msg": msg, "level": ev["level"], "slot": self.slot, "node": node,
                            "kind": kind})

    def _set(self, state, error=""):
        with self.lock:
            self.state, self.error = state, error

    def _chk(self, gen):
        if gen != self.gen:
            raise Cancelled()

    def resolve_model(self, name):
        root = Path(self.cfg.models_dir).resolve()
        p = (root / name).resolve()
        if root not in p.parents or p.suffix != ".gguf" or not p.is_file():
            raise ValueError(f"modelo no válido o fuera de {root}: {name}")
        return str(p)

    def list_models(self):
        return self.models.list()

    def delete_model(self, name):
        if name == self.model and self.state != IDLE:
            raise Busy("ese modelo está cargado: descárgalo antes de eliminarlo")
        self.models.delete(name)
        self.event(f"modelo eliminado: {name}")

    # ---------------------------------------------------------- planificación
    def bench_all(self, nodes, force):
        # SECUENCIAL a propósito: en paralelo los nodos se repartirían el enlace del orquestador
        # y todos parecerían más lentos de lo que son.
        for n in nodes:
            if not force and n.net and time.time() - n.net["at"] < 60:
                continue
            try:
                n.net = bench_node(self.reg.secret, n)
            except Exception as e:
                n.net, n.last_error = None, f"bench: {e}"[:200]

    # ---------------------------------------------------------- ajustes por nodo (en el orquestador)
    def models_name(self, mpath):
        try:
            return str(Path(mpath).resolve().relative_to(self.models.root)).replace(os.sep, "/")
        except ValueError:
            return os.path.basename(mpath)

    def _node_file(self):
        return config.home() / "nodes.json"

    def all_node_settings(self):
        try:
            return json.loads(self._node_file().read_text())
        except (OSError, ValueError):
            return {}

    def node_settings(self, name):
        return self.all_node_settings().get(name, {})

    def set_node_settings(self, req):
        n = self.reg.get(req.get("node") or "")
        if not n:
            raise ValueError("nodo desconocido")
        cur = self.node_settings(n.name)
        if "enabled" in req:
            if not isinstance(req["enabled"], bool):
                raise ValueError("'enabled' debe ser true o false")
            cur["enabled"] = req["enabled"]
        if "direct_local" in req:  # True = valor por defecto (se puede ejecutar directo en este equipo)
            if req["direct_local"] not in (True, False, None):
                raise ValueError("'direct_local' debe ser true o false")
            if req["direct_local"] is False:
                cur["direct_local"] = False
            else:
                cur.pop("direct_local", None)
        if "allow_slow" in req:
            if not isinstance(req["allow_slow"], bool):
                raise ValueError("'allow_slow' debe ser true o false")
            if req["allow_slow"] and req.get("confirm") is not True:
                raise ValueError("usar un nodo con red lenta requiere confirmación (confirm: true)")
            cur["allow_slow"] = req["allow_slow"]
            if not req["allow_slow"]:
                cur.pop("allow_slow", None)
        for k, lo, hi, typ in (("layers", 1, 4096, int), ("bw_gbps", 1.0, 10000.0, float)):
            if k in req:
                v = req[k]
                if v in (None, ""):
                    cur.pop(k, None)
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"'{k}' debe ser un número")
                if typ is int and f != int(f) or not lo <= f <= hi:
                    raise ValueError(f"'{k}' debe ser {'un entero ' if typ is int else ''}entre {lo:g} y {hi:g}")
                cur[k] = typ(f)
        allns = self.all_node_settings()
        allns[n.name] = cur
        self._node_file().parent.mkdir(parents=True, exist_ok=True)
        self._node_file().write_text(json.dumps(allns, indent=2, ensure_ascii=False))
        self.event(f"ajustes de {n.name}: {cur or 'automático'}"
                   + (" (se aplican al recargar el modelo)" if n.id in self.used else ""))
        return {"ok": True, "settings": cur, "in_use": n.id in self.used}

    # ---------------------------------------------------------- métricas de llama-server
    def metrics_loop(self):
        """Lee /metrics de llama-server (Prometheus) y calcula tokens/s en directo."""
        prev = None
        while not self.dead:
            time.sleep(2)
            if self.state != READY or not self.llama_port:
                prev = None
                continue
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.llama_port, timeout=2)
                c.request("GET", "/metrics")
                r = c.getresponse()
                text = r.read().decode("utf-8", "replace") if r.status == 200 else ""
                c.close()
            except OSError:
                continue
            m = parse_prometheus(text)
            now = time.time()
            gen, prompt = m.get("llamacpp:tokens_predicted_total"), m.get("llamacpp:prompt_tokens_total")
            s = {"t": now, "requests": m.get("llamacpp:requests_processing"),
                 "deferred": m.get("llamacpp:requests_deferred"), "kv_usage": m.get("llamacpp:kv_cache_usage_ratio"),
                 "tok_s": None, "prompt_tok_s": None, "tokens_total": gen}
            if prev and gen is not None and prev.get("tokens_total") is not None:
                dt = now - prev["t"]
                s["tok_s"] = round(max(0.0, gen - prev["tokens_total"]) / dt, 2)
                if prompt is not None and prev.get("prompt_total") is not None:
                    s["prompt_tok_s"] = round(max(0.0, prompt - prev["prompt_total"]) / dt, 1)
            s["prompt_total"] = prompt
            prev = s
            self.llama_hist.append(s)

    def telemetry(self, since=None):
        """Con `since` (marca de tiempo de la respuesta anterior) solo envía las muestras nuevas: el panel pide cada 2 s
        y el historial completo (150 muestras por nodo) se mandaba entero cada vez."""
        def newer(seq):
            return [t for t in seq if since is None or t["t"] > since]

        def compact(t):
            g = t.get("gpu") or {}
            return [round(t["t"], 1), g.get("util"), g.get("vram_used"), g.get("power"), g.get("temp"),
                    t.get("cpu"), (t.get("rpc_rx_bps") or 0) + (t.get("rpc_tx_bps") or 0)]
        nodes = []
        for n in self.reg.all():
            i = n.info
            nodes.append({"id": n.id, "name": n.name, "up": self.reg.is_up(n), "used": any(n.id in c.used for c in list(self.slots.values())),
                          "backend": i.get("backend"), "device_desc": i.get("device_desc"), "gpu_warning": i.get("gpu_warning"), "cores": i.get("cores"),
                          "threads": i.get("threads"), "cap_bytes": i.get("cap_bytes"),
                          "settings": self.node_settings(n.name), "now": i.get("telemetry") or {},
                          "models": self.node_models(n),
                          "hist": [compact(t) for t in newer(list(n.hist))],
                          "hist_cols": ["t", "gpu_util", "vram_used", "power", "temp", "cpu", "rpc_bps"]})
        lh = list(self.llama_hist)
        return {"t": time.time(), "delta": since is not None, "state": self.state, "model": self.model,
                "llama": lh[-1] if lh else {}, "llama_hist": [[round(x["t"], 1), x.get("tok_s"), x.get("requests")]
                                                             for x in newer(lh)], "nodes": nodes}

    def resources(self, extra=None, skip=()):
        """Recursos del clúster: VRAM, RAM y núcleos por nodo y en total, con lo ya reservado por los modelos cargados.
        `extra` = {"vram": {nodo: bytes}, "ram": {nodo|@host: bytes}} (la reserva de un plan simulado): añade los
        valores «tras cargar». Sirve para ver cuánto consumiría un modelo sin cargarlo."""
        ex = extra or {}
        exv, exr = ex.get("vram") or {}, ex.get("ram") or {}
        nodes, tot = [], {k: 0 for k in ("vram_cap", "vram_reserved", "vram_after", "ram_total", "ram_free",
                                         "ram_reserved", "ram_after", "cores", "threads")}
        local = False
        for n in self.reg.all():
            i, up = n.info, self.reg.is_up(n)
            gpu = i.get("backend", "cpu") != "cpu"
            cap = int(i.get("cap_bytes") or 0)
            ram_total = int(i.get("ram_total") or (0 if gpu else cap))
            vres = self.ledger.reserved(n.id, "vram", skip)
            rres = self.ledger.reserved(n.id, "ram", skip)
            row = {"id": n.id, "name": n.name, "up": up, "gpu": gpu, "backend": i.get("backend"),
                   "device_desc": i.get("device_desc"), "cpu_device": bool(i.get("cpu_device")),
                   "vram_cap": cap if gpu else 0, "vram_reserved": vres if gpu else 0,
                   "vram_after": (vres if gpu else 0) + int(exv.get(n.id, 0)),
                   "ram_total": ram_total, "ram_free": int(i.get("ram_free") or 0), "ram_reserved": rres,
                   "ram_after": rres + int(exr.get(n.id, 0)),
                   "cores": i.get("cores") or 0, "threads": i.get("threads") or 0,
                   "net": {k: (n.net or {}).get(k) for k in ("mbps", "rtt_ms", "tier")}}
            if not gpu:  # nodo solo-CPU: su aporte es RAM
                row["ram_cap"] = cap
            if is_local_host(n.host):
                local = True
            nodes.append(row)
            if up:
                for k in ("vram_cap", "vram_reserved", "vram_after", "ram_total", "ram_free", "ram_reserved",
                          "ram_after", "cores", "threads"):
                    tot[k] += row[k]
        total_h, avail_h = hw.mem_info()
        host = {"name": "orquestador", "ram_total": total_h, "ram_free": avail_h,
                "ram_reserved": self.ledger.reserved(HOST, "ram", skip),
                "ram_after": self.ledger.reserved(HOST, "ram", skip) + int(exr.get(HOST, 0)),
                "cores": os.cpu_count() or 0, "shared_with_node": local}
        if not local:  # el orquestador también aporta su RAM y sus núcleos (modelos en CPU)
            for k in ("ram_total", "ram_free", "ram_reserved", "ram_after", "cores"):
                tot[k] += host[k]
        return {"nodes": nodes, "host": host, "totals": tot}

    def host_free_ram(self, skip):
        """RAM utilizable en el equipo del orquestador: la libre de verdad − margen − reservas de otros slots."""
        total, avail = hw.mem_info()
        return self.ledger.available_ram(HOST, total, avail, skip)

    @staticmethod
    def parse_targets(targets):
        """[{node, device}] → {nodo: 'gpu'|'cpu'}; None/[] = automático. Valida el formato."""
        if not targets:
            return {}
        if not isinstance(targets, list) or len(targets) > 32:
            raise ValueError("targets debe ser una lista de {node, device}")
        out = {}
        for t in targets:
            if not isinstance(t, dict) or not isinstance(t.get("node"), str) or not t["node"]:
                raise ValueError("cada destino necesita «node»")
            dev = str(t.get("device") or "gpu").lower()
            if dev not in ("gpu", "cpu"):
                raise ValueError(f"dispositivo no válido para {t['node']}: use gpu o cpu")
            out[t["node"]] = dev
        return out

    def make_plan(self, mpath, ctx, force_bench=True, ignore=(), targets=None, own=True):
        """Plan de reparto. `ignore`: slots cuya memoria se da por liberada (¿cabría si los expulsara?).
        `targets`: [{node, device}] para elegir a mano dónde corre (None = el elegido para este slot; [] = automático).
        «@host» es la RAM del propio orquestador y no se combina con nodos."""
        m = gguf.read_model(mpath)
        ms = self.models.settings(self.models_name(mpath))
        skip = ({self.slot} if own else set()) | set(ignore)  # own=False: un slot nuevo, no sustituye a este
        kt, vt = ms.get("cache_type_k"), ms.get("cache_type_v")
        policy = ms.get("device_policy", "auto")
        tmap = self.parse_targets(self.targets if targets is None else targets)
        if HOST in tmap:
            if len(tmap) > 1:
                p = scheduler.plan(m, [], ctx, self.cfg.min_mbps, kt, vt)
                p["reason"] = ("el orquestador (@host) no se puede combinar con nodos; para repartir entre GPU y la RAM "
                               "del orquestador usa la política «gpu_with_cpu_fallback»")
                return m, p
            policy = "cpu_only"
        if policy == "cpu_only":  # sin nodos remotos: todo en RAM del orquestador
            p = scheduler.host_plan(m, ctx, self.host_free_ram(skip), kt, vt, "cpu_only")
            p["reserve"] = {"vram": {}, "ram": {HOST: p["host_ram"]}} if p["ok"] else {}
            p["placement"] = "host"
            return m, p
        nodes, excluded, ok_nodes, excluded_note = self.reg.all(), [], [], []
        up = [n for n in nodes if self.reg.is_up(n)]
        excluded += [{"node": n.name, "reason": "sin respuesta"} for n in nodes if n not in up]
        if tmap:
            known = {n.name for n in nodes}
            excluded += [{"node": nm, "reason": "no existe"} for nm in tmap if nm not in known]
            excluded += [{"node": n.name, "reason": "no seleccionado"} for n in up if n.name not in tmap]
            up = [n for n in up if n.name in tmap]
        off = [n for n in up if self.node_settings(n.name).get("enabled") is False]
        excluded += [{"node": n.name, "reason": "desactivado en el panel"} for n in off]
        up = [n for n in up if n not in off]
        self.bench_all(up, force_bench)
        for n in up:
            i = n.info
            gpu_native = i.get("backend", "cpu") != "cpu"
            dev = tmap.get(n.name)           # None: automático; 'gpu' o 'cpu': elegido a mano
            gpu = gpu_native and dev != "cpu"
            if dev == "gpu" and not gpu_native:
                excluded.append({"node": n.name, "reason": "se pidió GPU y el nodo no tiene"})
            elif dev == "cpu" and gpu_native and not i.get("cpu_device"):
                excluded.append({"node": n.name, "reason": "se pidió CPU y su agente no la ofrece (actualízalo)"})
            elif not i.get("rpc_bin"):
                excluded.append({"node": n.name, "reason": "rpc-server no instalado"})
            elif self.llama_build and i.get("rpc_build") and not same_build(i["rpc_build"], self.llama_build):
                up_ = i.get("update") or {}
                if up_.get("state") == "descargando":
                    why = f"actualizando llama.cpp a {up_.get('tag')} ({up_.get('progress', 0)} %)"
                elif up_.get("state") == "error":
                    why = f"build distinta y la actualización falló: {up_.get('error')}"
                else:
                    why = (f"build distinta ({i['rpc_build']} ≠ {self.llama_build})"
                           + ("; se le pedirá actualizar" if i.get("auto_update") and self.cfg_auto_sync()
                              else ": reinstala el nodo con el código de unión actual"))
                excluded.append({"node": n.name, "reason": why})
            elif self.slot and "rpc_slots" not in i:
                excluded.append({"node": n.name, "reason": "su agente es anterior a 0.5.0 y no admite varios "
                                                           "modelos a la vez: actualízalo"})
            elif policy in ("gpu_strict", "gpu_with_cpu_fallback") and not gpu:
                excluded.append({"node": n.name, "reason": f"{policy}: el nodo no tiene GPU"})
            elif gpu and self.ledger.available_ram(n.id, i.get("ram_total") or 0, i.get("ram_free"), skip) \
                    < scheduler.IO_RAM and i.get("ram_total"):
                excluded.append({"node": n.name, "reason": "queda muy poca RAM libre en el nodo (margen de seguridad)"})
            else:
                ns = self.node_settings(n.name)
                cpu_on_gpu = gpu_native and not gpu  # GPU del nodo libre: el modelo usa su RAM
                cap0 = int(i.get("ram_total") or 0) if cpu_on_gpu else i.get("cap_bytes", 0)
                cap = self.ledger.available_pool(n.id, cap0, "vram" if gpu else "ram", skip)
                if cpu_on_gpu and i.get("ram_free") is not None:
                    cap = min(cap, max(0, int(i["ram_free"]) - self.ledger.ram_margin))
                held = self.ledger.reserved(n.id, "vram" if gpu else "ram", skip)
                if held:
                    excluded_note.append(f"{n.name}: {held / hw.GiB:.1f} GiB reservados por otros modelos")
                ok_nodes.append({"id": n.id, "name": n.name, "backend": "cpu" if cpu_on_gpu else i.get("backend", "cpu"),
                                 "gpu": gpu, "device": "gpu" if gpu else "cpu", "cap_bytes": cap,
                                 "bw_gbps": ns.get("bw_gbps") or (CPU_BW_GBPS if cpu_on_gpu else i.get("bw_gbps", 10)),
                                 "layers": ns.get("layers"), "allow_slow": bool(ns.get("allow_slow")),
                                 "mbps": n.net["mbps"] if n.net else None,
                                 "rtt_ms": n.net["rtt_ms"] if n.net else None,
                                 "local": n.net.get("local") if n.net else None,
                                 "tier": n.net.get("tier") if n.net else None})
        place = ms.get("placement", "auto")
        if place == "auto":
            place = engine.driver_for(ms).default_placement()
        mx = 1 if place == "node" else None
        p = scheduler.plan(m, ok_nodes, ctx, self.cfg.min_mbps, kt, vt, max_nodes=mx)
        p["policy"] = policy
        if not p["ok"] and policy == "gpu_with_cpu_fallback":
            p = self._fallback_plan(m, ok_nodes, ctx, kt, vt, mx, skip, p)
        p["placement"] = place
        p["excluded"] = excluded + p["excluded"]
        if excluded_note:
            p["warnings"] = p.get("warnings", []) + ["memoria ya reservada: " + "; ".join(excluded_note)]
        if p["ok"]:
            by = {n["id"]: n for n in ok_nodes}
            vr, rr = {}, {}
            for a in p["assignments"]:
                a["device"] = by[a["node_id"]]["device"]
                if by[a["node_id"]].get("gpu"):
                    vr[a["node_id"]] = a["bytes"] + scheduler.OVERHEAD_NODE
                    rr[a["node_id"]] = scheduler.IO_RAM
                else:
                    rr[a["node_id"]] = a["bytes"] + scheduler.OVERHEAD_NODE
            if p.get("host_ram"):
                rr[HOST] = p["host_ram"]
            p["reserve"] = {"vram": vr, "ram": rr}
        return m, p

    def _fallback_plan(self, m, ok_nodes, ctx, kt, vt, mx, skip, full):
        """gpu_with_cpu_fallback: lo que quepa en VRAM (-ngl N) y el resto en la RAM del orquestador; si ni una capa
        cabe en GPU, todo en CPU (-ngl 0)."""
        gpu_nodes = [n for n in ok_nodes if n.get("gpu")]
        usable = sum(max(0, (n["cap_bytes"] - scheduler.OVERHEAD_NODE) * scheduler.HEADROOM) for n in gpu_nodes)
        n_gpu = scheduler.offload_layers(m, ctx, usable, kt, vt) if gpu_nodes and m.get("n_layers") else 0
        free = self.host_free_ram(skip)
        if n_gpu >= 1:
            sub = scheduler.sub_model(m, n_gpu)
            p2 = scheduler.plan(sub, gpu_nodes, ctx, self.cfg.min_mbps, kt, vt, max_nodes=mx)
            if p2["ok"]:
                host = scheduler.need_bytes(m, ctx, kt, vt) - p2["need_bytes"]
                if host <= free:
                    p2.update(ngl=n_gpu, host_ram=host, policy="gpu_with_cpu_fallback", model=m["name"],
                              n_layers_total=m.get("n_layers"), model_bytes=m["size"])
                    p2["warnings"].append(f"solo {n_gpu} de {m['n_layers']} capas caben en GPU: el resto "
                                          f"({host / hw.GiB:.1f} GiB) corre en la CPU del equipo del orquestador")
                    return p2
        hp = scheduler.host_plan(m, ctx, free, kt, vt, "gpu_with_cpu_fallback")
        if hp["ok"]:
            hp["warnings"].insert(0, f"no cabe en GPU ({full['reason']}): se ejecuta entero en CPU (-ngl 0)")
            hp["excluded"] = full.get("excluded", [])
            return hp
        full["reason"] += f" · alternativa en CPU: {hp['reason']}"
        return full

    # ---------------------------------------------------------- carga / descarga
    def request_load(self, name, ctx, pinned=None, targets=None):
        path = self.resolve_model(name)
        self.parse_targets(targets)  # valida antes de tocar nada
        for k, c in list(self.slots.items()):
            if k != self.slot and c.model == name and c.state in (LOADING, READY, RECOVERING):
                raise Busy(f"{name} ya está cargado en el slot {k}")
        if pinned is not None:
            self.pinned = bool(pinned)
        with self.lock:
            if self.state == LOADING:
                raise Busy("ya hay una carga en curso")
            self.gen += 1
            gen = self.gen
            self.desired = (name, ctx)
            self.targets = targets or None
        threading.Thread(target=self._load_worker, args=(path, name, ctx, gen, False), daemon=True).start()

    # ---------------------------------------------------------- slots (varios modelos a la vez)
    def new_slot(self, pinned=True):
        """Crea un slot vacío (solo en el coordinador). Cada slot tiene su llama-server, sus rpc-server y su túnel."""
        with self.lock:
            free = [i for i in range(1, auth.MAX_SLOTS) if i not in self.slots]
            if not free:
                raise Busy(f"máximo de {auth.MAX_SLOTS} modelos simultáneos")
            c = Cluster(self.cfg, self.reg, slot=free[0], parent=self, pinned=pinned)
            self.slots[free[0]] = c
        for fn in (c.watchdog, c.metrics_loop):
            threading.Thread(target=fn, daemon=True).start()
        return c

    def load_model(self, name, ctx, slot=0, pinned=True, targets=None):
        """Carga `name` en un slot: 0 (principal), un número existente o «new» (slot nuevo).
        targets: [{node, device}] para elegir nodos y GPU/CPU a mano (vacío = automático)."""
        if slot in (None, 0, "0"):
            self.slots[0].request_load(name, ctx, pinned, targets)
            return 0
        if slot == "new":
            c = self.new_slot(pinned)
            try:
                c.request_load(name, ctx, pinned, targets)
            except Exception:
                self.drop_slot(c.slot)
                raise
            return c.slot
        c = self.slots.get(int(slot))
        if c is None:
            raise ValueError(f"el slot {slot} no existe")
        c.request_load(name, ctx, pinned, targets)
        return c.slot

    def drop_slot(self, slot):
        """Descarga el slot; los secundarios además desaparecen (el 0 queda vacío)."""
        c = self.slots.get(int(slot))
        if c is None:
            raise ValueError(f"el slot {slot} no existe")
        t = c.unload()
        if c.slot:
            def done():
                if t:
                    t.join(60)
                c.dead = True
                with self.lock:
                    self.slots.pop(c.slot, None)
            if t:  # cancelando una carga: el slot desaparece cuando termine la limpieza
                threading.Thread(target=done, daemon=True).start()
            else:
                done()

    # ---------------------------------------------------------- enrutado L7 por modelo y capacidad
    @staticmethod
    def _names(c):
        n = c.model or ""
        base = os.path.basename(n)
        return {n.lower(), base.lower(), (base[:-5] if base.endswith(".gguf") else base).lower()}

    def find_slot(self, model, states=(READY,)):
        m = (model or "").lower()
        for k in sorted(self.slots):
            c = self.slots[k]
            if c.state in states and c.model and m in self._names(c):
                return c
        return None

    def default_slot(self, cap):
        """Slot listo con esa capacidad: el principal si vale; si no, el usado más recientemente."""
        ok = [c for c in self.slots.values() if c.state == READY and (cap is None or cap in c.driver.capabilities())]
        if not ok:
            return None
        return next((c for c in ok if c.slot == 0), None) or max(ok, key=lambda c: c.last_used)

    def disk_model(self, model):
        """Nombre de un .gguf de la carpeta de modelos que corresponde a `model` (o None)."""
        m = (model or "").lower()
        if not m:
            return None
        for it in self.list_models():
            nm = it["name"]
            base = os.path.basename(nm)
            if m in (nm.lower(), base.lower(), (base[:-5] if base.endswith(".gguf") else base).lower()):
                return nm
        return None

    def route(self, model, cap):
        """Elige el slot que atiende la petición. Lanza RouteError(estado, mensaje)."""
        deadline = time.time() + getattr(self.cfg, "queue_timeout", 30.0)
        c = self.find_slot(model)
        if c is None and model and getattr(self.cfg, "on_demand", False):
            nm = self.disk_model(model)
            if nm and not self.find_slot(nm, (LOADING, RECOVERING)):
                c = self.load_on_demand(nm, deadline)
            elif nm:
                c = self.wait_slot(nm)
        if c is None:
            c = self.default_slot(cap)  # nombre desconocido o ausente: como siempre, el modelo cargado
        if c is None:
            st = self.slots[0].state
            raise RouteError(503, f"modelo no listo (estado {st})")
        if cap and cap not in c.driver.capabilities():
            raise RouteError(400, f"el modelo {c.model} no admite {cap} (admite: {', '.join(sorted(c.driver.capabilities()))})")
        return c

    def wait_slot(self, name, timeout=None):
        end = time.time() + (timeout or self.cfg.load_timeout + 60)
        while time.time() < end:
            c = self.find_slot(name, (READY,))
            if c:
                return c
            if not self.find_slot(name, (LOADING, RECOVERING)):
                raise RouteError(503, f"no se pudo cargar {name}")
            time.sleep(0.3)
        raise RouteError(504, f"{name} no estuvo listo a tiempo")

    def lru_victim(self, exclude=None):
        """Slot on-demand listo y sin peticiones en curso que lleva más tiempo sin usarse."""
        cand = [c for c in self.slots.values() if c is not exclude and not c.pinned and c.state == READY
                and c.inflight == 0]
        return min(cand, key=lambda c: c.last_used) if cand else None

    def load_on_demand(self, name, deadline=None):
        """Carga `name` en un slot nuevo y espera a que esté listo.

        - Si falta memoria, expulsa por LRU los slots no fijados, listos y SIN peticiones en curso (nunca mata una
          generación a medias).
        - Si el modelo no cabría ni expulsando todo lo que no está fijado, se rechaza ANTES de lanzar nada (503).
        - Si cabría pero los demás están ocupados, la petición espera en una cola acotada por `queue_timeout`;
          vencido el plazo, 429 con Retry-After. La cola es el propio cerrojo: una carga a la vez."""
        deadline = deadline or time.time() + getattr(self.cfg, "queue_timeout", 30.0)
        if not self.ondemand_lock.acquire(timeout=max(0.0, deadline - time.time())):
            raise RouteError(429, "hay otra carga de modelo en curso y se agotó el tiempo de espera de la cola",
                             retry_after=5)
        slot = None
        try:
            c = self.find_slot(name)
            if c:
                return c
            ctx = int(self.models.settings(name).get("ctx") or 4096)
            path = self.resolve_model(name)
            slot = self.new_slot(pinned=False)
            while True:
                _, p = slot.make_plan(path, ctx, force_bench=False)
                if p["ok"]:
                    break
                movable = [c for c in self.slots.values() if c is not slot and not c.pinned
                           and c.state in (READY, LOADING, RECOVERING)]
                # rechazo previo: si no cabe ni sin ningún modelo no fijado, no se expulsa nada en vano
                _, p2 = slot.make_plan(path, ctx, force_bench=False, ignore={c.slot for c in movable})
                if not p2["ok"]:
                    raise RouteError(503, f"{name} no cabe ni descontando los modelos fijados: {p2['reason']}")
                victim = self.lru_victim(exclude=slot)
                if victim is not None:
                    self.event(f"memoria insuficiente para {name}: se expulsa {victim.model} (LRU)")
                    self.drop_slot(victim.slot)
                    continue
                if time.time() >= deadline:
                    raise RouteError(429, f"todos los recursos están ocupados por modelos en uso; {name} espera "
                                          "a que alguno quede libre", retry_after=max(5, int(self.cfg.queue_timeout)))
                time.sleep(0.3)  # cola: reintenta cuando algún slot pase a IDLE
            self.event(f"carga bajo demanda: {name}")
            slot.request_load(name, ctx, False)
            end = time.time() + self.cfg.load_timeout + 60
            while slot.state in (IDLE, LOADING, RECOVERING) and time.time() < end:
                time.sleep(0.3)
            if slot.state != READY:
                raise RouteError(503, f"no se pudo cargar {name}: {slot.error or slot.state}")
            return slot
        except Exception:
            if slot is not None and slot.slot in self.slots and slot.state != READY:
                self.drop_slot(slot.slot)
            raise
        finally:
            self.ondemand_lock.release()

    def test_tools(self, slot):
        """Prueba real de function calling contra un modelo cargado: le pide usar una herramienta de ejemplo."""
        c = self.slots.get(int(slot))
        if not c or c.state != READY or not c.llama_port:
            raise ValueError("el modelo no está listo")
        if "embeddings" in c.driver.capabilities():
            raise ValueError("un modelo de embeddings no usa herramientas")
        body = {"model": c.model, "temperature": 0, "max_tokens": 200, "stream": False,
                "messages": [{"role": "user", "content": "¿Qué tiempo hace ahora mismo en Madrid? Usa la herramienta."}],
                "tools": [{"type": "function", "function": {
                    "name": "get_weather", "description": "Devuelve el tiempo actual de una ciudad",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
                "tool_choice": "auto"}
        t0 = time.time()
        cn = http.client.HTTPConnection("127.0.0.1", c.llama_port, timeout=180)
        try:
            cn.request("POST", "/v1/chat/completions", body=json.dumps(body), headers={"Content-Type": "application/json"})
            r = cn.getresponse()
            raw = r.read()
        except OSError as e:
            raise ValueError(f"el modelo no respondió: {e}")
        finally:
            cn.close()
        out = {"slot": c.slot, "model": c.model, "jinja": c.tools, "tool_template": c.tool_template,
               "seconds": round(time.time() - t0, 1), "status": r.status}
        try:
            msg = json.loads(raw)["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            out.update(ok=False, detail=f"respuesta no válida ({r.status}): {raw[:200].decode('utf-8', 'replace')}")
            return out
        calls = msg.get("tool_calls") or []
        if calls and calls[0].get("function", {}).get("name") == "get_weather":
            try:
                args = json.loads(calls[0]["function"].get("arguments") or "{}")
            except ValueError:
                args = None
            out.update(ok=args is not None, arguments=args,
                       detail="el modelo llamó a la herramienta" + ("" if args is not None else " con argumentos no válidos"))
        else:
            out.update(ok=False, detail="el modelo respondió con texto en vez de llamar a la herramienta: "
                       + ("su plantilla no admite herramientas" if not c.tool_template else
                          "prueba con un modelo/cuantización mayor o revisa el prompt de sistema"),
                       sample=str(msg.get("content") or "")[:200])
        self.event(f"prueba de herramientas en {c.model}: {'correcta' if out['ok'] else 'sin llamada a herramienta'}",
                   level="info" if out["ok"] else "warn", kind="tools", detail={"slot": c.slot})
        return out

    def models_listing(self):
        data = []
        loaded = set()
        for k in sorted(self.slots):
            c = self.slots[k]
            if c.model and c.state == READY:
                loaded.add(c.model)
                data.append({"id": c.model, "object": "model", "owned_by": "dllm",
                             "dllm": {"slot": k, "state": c.state, "pinned": c.pinned,
                                      "activity": "BUSY" if c.inflight else "IDLE",
                                      "capabilities": sorted(c.driver.capabilities()
                                                             | ({"tools"} if c.tools and c.tool_template else set())
                                                             | ({"vision"} if c.mmproj else set()))}})
        if getattr(self.cfg, "on_demand", False):
            for it in self.list_models():
                if it["name"] not in loaded and not it["name"].startswith("mmproj"):
                    data.append({"id": it["name"], "object": "model", "owned_by": "dllm",
                                 "dllm": {"state": "bajo demanda"}})
        return {"object": "list", "data": data}

    @staticmethod
    def _live(c):
        """Último muestreo de /metrics de llama-server (tokens/s, uso de caché K/V, tokens totales)."""
        h = c.llama_hist[-1] if c.llama_hist else {}
        return {"tok_s": h.get("tok_s"), "prompt_tok_s": h.get("prompt_tok_s"), "kv_usage": h.get("kv_usage"),
                "tokens_total": h.get("tokens_total"), "prompt_tokens_total": h.get("prompt_total")}

    def slots_view(self):
        out = []
        for k in sorted(self.slots):
            c = self.slots[k]
            out.append({"slot": k, "model": c.model, "state": c.state, "error": c.error, "ctx": c.ctx,
                        "pinned": c.pinned, "last_used": c.last_used, "llama_port": c.llama_port,
                        "nodes": list(c.used), "est_tok_s": (c.plan or {}).get("est_tok_s"),
                        "active_requests": c.inflight, "activity": "BUSY" if c.inflight else "IDLE",
                        "policy": (c.plan or {}).get("policy"),
                        "reserved": dict(self.ledger.res.get(k, {})),
                        "targets": c.targets, "recoveries": c.recoveries,
                        "direct": c.direct["desc"] if c.direct else None,
                        "tools": c.tools, "tool_template": c.tool_template, "vision": bool(c.mmproj),
                        "assignments": [{k2: a.get(k2) for k2 in ("name", "layers", "bytes", "device", "backend")}
                                        for a in (c.plan or {}).get("assignments", [])],
                        "metrics": {**c.metrics_req.summary(), **self._live(c)}})
        return out

    def unload(self):
        """Descarga el modelo. Si hay una carga en curso la CANCELA sin esperar (devuelve un hilo que termina la
        limpieza); si no, descarga y devuelve None."""
        with self.lock:
            self.gen += 1
            self.desired = None
            busy = self.state in (LOADING, RECOVERING)
        if busy:
            p = self.proc
            if p and p.poll() is None:
                try:
                    p.terminate()  # que la espera de la carga se entere ya
                except OSError:
                    pass
            self._set(UNLOADING)

        def finish():
            with self.work:
                self._teardown()
                self.model, self.plan = None, None
                self._set(IDLE)
            self.event("carga cancelada" if busy else "modelo descargado", kind="unload")
        if busy:
            t = threading.Thread(target=finish, daemon=True)
            t.start()
            return t
        finish()
        return None

    def _load_worker(self, path, name, ctx, gen, recovering):
        with self.work:
            if gen != self.gen:
                return
            self._teardown()
            self.model, self.ctx = name, ctx
            self._set(RECOVERING if recovering else LOADING)
            self.last_nodeset = self._nodesig()
            try:
                if not self.cfg.llama_bin:
                    raise LoadError("llama-server no encontrado (--bin-dir / --llama-bin)")
                self.event(f"{'recuperando' if recovering else 'cargando'} {name} (ctx {ctx})")
                if recovering:
                    time.sleep(1.5)  # deja que los agentes liberen memoria antes de medir capacidad
                with self.ledger.plan_lock:  # planificar y reservar de una vez: dos cargas no ven la misma memoria
                    _, plan = self.make_plan(path, ctx)
                    self._chk(gen)
                    self.plan = plan
                    if not plan["ok"]:
                        raise LoadError(plan["reason"])
                    rsv = plan.get("reserve") or {}
                    self.ledger.reserve(self.slot, rsv.get("vram"), rsv.get("ram"))
                used = [self.reg.get(a["node_id"]) for a in plan["assignments"]]
                self.direct = self._direct_device(plan, used)
                if self.direct:
                    self.event(f"el modelo cabe en la GPU local ({self.direct['desc']}): se ejecuta directamente, sin RPC",
                               kind="load")
                else:
                    for n in used:
                        self.event(f"arrancando rpc-server en {n.name}" + (f" (slot {self.slot})" if self.slot else ""))
                        dev = next((a.get("device") for a in plan["assignments"] if a["node_id"] == n.id), None)
                        call_agent(self.reg.secret, n, "POST", "/rpc/start", {"slot": self.slot, "device": dev},
                                   timeout=40)
                        self._chk(gen)
                self.used = [n.id for n in used]
                self.tunnels = [] if self.direct else [Tunnel(n.host, n.rpc_port, self.reg.secret, self.slot).start()
                                                       for n in used]
                self._launch_llama(path, ctx, plan)
                self._wait_ready(gen)
                self._set(READY)
                self.event(f"{name} listo en {len(used)} nodo(s)"
                           + (f" · ~{plan['est_tok_s']} tok/s estimados" if plan.get("est_tok_s") else
                              " · en CPU del orquestador"))
            except Cancelled:
                self._teardown()
            except Exception as e:
                self.event(f"error: {e}")
                self._teardown()
                self._set(ERROR, str(e))

    def _direct_device(self, plan, used):
        """GPU local en la que ejecutar el modelo sin RPC, o None. Solo si el reparto es un único nodo que es este mismo
        equipo, en GPU, y llama-server local ve esa GPU. Evita el túnel y rpc-server; los demás modelos siguen
        pudiendo usar el resto de nodos (y esta GPU: la reserva de VRAM es la misma).
        Se puede decidir en tres niveles (de más a menos específico): ajuste del modelo `local_gpu` (auto|on|off),
        ajuste del nodo `direct_local` (false = nunca directo en este equipo) y la opción global --no-direct-local.
        `on` obliga: si no es posible, la carga falla con el motivo en vez de caer en silencio a RPC."""
        mode = self.models.settings(self.model).get("local_gpu", "auto")
        dev, why = self._direct_candidate(plan, used)
        if mode == "off":
            if dev:
                self.event("ejecución directa en GPU local desactivada para este modelo (local_gpu=off): se usa RPC")
            return None
        if mode == "on":
            if not dev:
                raise LoadError(f"local_gpu=on: no se puede ejecutar directo en la GPU local ({why}). "
                                "Pon el modelo en 'auto' o fija un destino que sea la GPU de este equipo.")
            return dev
        if dev:
            n = used[0]
            if getattr(self.cfg, "no_direct_local", False):
                self.event("ejecución directa desactivada (--no-direct-local): se usa RPC")
                return None
            if self.node_settings(n.name).get("direct_local") is False:
                self.event(f"ejecución directa desactivada en el nodo {n.name} (ajuste del nodo): se usa RPC")
                return None
        return dev

    def _direct_candidate(self, plan, used):
        """(dispositivo, motivo): ¿puede este plan ejecutarse directo en la GPU local? Sin mirar las preferencias."""
        if len(used) != 1 or not used[0]:
            return None, "el reparto usa más de un nodo"
        n, a = used[0], plan["assignments"][0]
        if a.get("device") == "cpu" or a.get("backend") == "cpu":
            return None, "el reparto es en CPU"
        if not is_local_host(n.host):
            return None, f"el nodo {n.name} no es este equipo"
        try:
            if self._local_devs is None:
                self._local_devs = devices.probe_llama_server(self.cfg.llama_bin)
        except RuntimeError as e:
            return None, f"no se pudo listar las GPU de llama-server ({e})"
        want = ((n.info or {}).get("device_desc") or "").lower(), ((n.info or {}).get("device") or "").lower()
        for d in self._local_devs:
            if not devices.is_cpu(d) and (d["desc"].lower() == want[0] and want[0] or d["name"].lower() == want[1] and want[1]):
                return d, ""
        return None, "llama-server local no ve la GPU de ese nodo"

    def _launch_llama(self, path, ctx, plan):
        self.llama_port = hw.slot_port({c.llama_port for c in self.slots.values() if c is not self})
        st = self.models.settings(self.model)
        self.driver = engine.driver_for(st)
        self.mmproj = None
        mp = None
        if "embeddings" not in self.driver.capabilities():  # un modelo de embeddings no lleva proyector
            try:
                mp = self.models.mmproj_for(self.model)
            except (models.ModelError, OSError, ValueError) as e:  # un proyector roto no impide cargar el texto
                self.event(f"proyector multimodal no disponible ({e}): se carga solo texto")
        if mp:
            self.mmproj = os.path.basename(mp)
            self.event(f"multimodal: proyector {self.mmproj}")
        info = self.models.list_one(self.model) if hasattr(self.models, "list_one") else {}
        mode = st.get("tools", "auto")
        self.tools = mode == "on" or (mode == "auto" and bool(info.get("chat_template")))
        self.tool_template = bool(info.get("tool_template"))
        if self.tools and not self.tool_template:
            self.event("aviso: la plantilla de este modelo no parece admitir herramientas (tool calling); se activa "
                       "--jinja pero puede que no devuelva tool_calls", level="warn", kind="load")
        cmd = self.driver.build_command({
            "jinja": self.tools, "bin": self.cfg.llama_bin, "model_path": path, "port": self.llama_port, "ctx": ctx,
            "rpc": [f"127.0.0.1:{t.port}" for t in self.tunnels],
            "tensor_split": None if self.direct else plan["tensor_split"], "ngl": plan.get("ngl", 999),
            "device_args": (["--device", self.direct["name"]] if self.direct
                            else self._device_args(plan) if self.tunnels else []),
            "mmproj": mp, "extra": self.cfg.llama_arg or []})
        self.event("lanzando: " + " ".join(cmd))
        self.logbuf.clear()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=lambda p: [self.logbuf.append(l.rstrip()) for l in p.stdout],
                         args=(self.proc,), daemon=True).start()

    def _device_args(self, plan):
        """Fija los dispositivos de llama-server a los remotos (uno por nodo, en el orden de --rpc).

        Se listan los dispositivos sin y con --rpc: la diferencia son los remotos. Así la GPU local del orquestador no
        entra en el reparto (rol Ambos en un PC con GPU) y no dependemos de cómo nombre llama.cpp a los RPC."""
        rpc = [f"127.0.0.1:{t.port}" for t in self.tunnels]
        try:
            if self._local_devs is None:
                self._local_devs = devices.probe_llama_server(self.cfg.llama_bin)
            rem = devices.remote(self._local_devs, devices.probe_llama_server(self.cfg.llama_bin, rpc))
        except RuntimeError as e:
            self.event(f"aviso: no se pudieron listar los dispositivos ({e}); se lanza sin --device")
            return []
        if not rem:
            self.event("aviso: esta versión de llama-server no lista los dispositivos remotos; se lanza sin --device")
            return []
        if len(rem) != len(rpc):
            raise LoadError(f"los nodos exponen {len(rem)} dispositivos y se esperaban {len(rpc)} (uno por nodo): "
                            "actualiza los agentes o fija el dispositivo de cada nodo con --device")
        for d, a in zip(rem, plan["assignments"]):
            if a["bytes"] > d["free"]:
                self.event(f"aviso: a {a['name']} se le asignan {a['bytes'] / hw.GiB:.1f} GiB y su dispositivo "
                           f"tiene {d['free'] / hw.GiB:.1f} GiB libres")
        self.event("dispositivos: " + ", ".join(f"{a['name']} → {d['desc']} ({d['free'] / hw.GiB:.1f} GiB libres)"
                                               for d, a in zip(rem, plan["assignments"])))
        return ["--device", ",".join(d["name"] for d in rem)]

    def _wait_ready(self, gen):
        deadline = time.time() + self.cfg.load_timeout
        while True:
            self._chk(gen)
            if self.proc.poll() is not None:
                raise LoadError("llama-server terminó: " + " | ".join(list(self.logbuf)[-6:]))
            if self.driver.healthcheck(self.llama_port):
                return
            if time.time() > deadline:
                raise LoadError(f"llama-server no estuvo listo en {self.cfg.load_timeout:.0f} s")
            time.sleep(0.5)

    def _teardown(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(8)
            except subprocess.TimeoutExpired:
                p.kill()
        for t in self.tunnels:
            t.stop()
        self.tunnels = []
        self.ledger.release(self.slot)
        for nid in ([] if self.direct else self.used):
            n = self.reg.get(nid)
            if n and self.reg.is_up(n):
                try:
                    call_agent(self.reg.secret, n, "POST", "/rpc/stop", {"slot": self.slot}, timeout=3)
                except Exception:
                    pass
        self.used, self.direct = [], None

    # ---------------------------------------------------------- vigilancia
    def cfg_auto_sync(self):
        return not getattr(self.cfg, "no_auto_sync", False)

    def sync_builds(self):
        """Pide a los nodos con otra versión de llama.cpp que instalen la del orquestador."""
        if not (self.cfg_auto_sync() and self.llama_build):
            return
        tag = installer.tag_from_build(self.llama_build)
        if not tag:
            return
        for n in self.reg.all():
            i = n.info
            if (not self.reg.is_up(n) or not i.get("auto_update") or not i.get("rpc_build")
                    or same_build(i["rpc_build"], self.llama_build) or (i.get("update") or {}).get("state") == "descargando"):
                continue
            if time.time() - self._sync_tried.get(n.id, 0) < 900:  # como mucho un intento cada 15 min
                continue
            self._sync_tried[n.id] = time.time()
            try:
                call_agent(self.reg.secret, n, "POST", "/llama/install", {"tag": tag}, timeout=10)
                self.event(f"{n.name}: tiene llama.cpp {i['rpc_build']}; se le pide instalar {tag}")
            except Exception as e:
                self.event(f"{n.name}: no se pudo pedir la actualización de llama.cpp ({e})")

    def _nodesig(self):
        return frozenset((n.id, n.info.get("rpc_build")) for n in self.reg.all() if self.reg.is_up(n))

    def watchdog(self):
        tick = 0
        while not self.dead:
            time.sleep(1)
            tick += 1
            if tick % 5 == 0 and not self.slot:
                try:
                    self.sync_builds()
                except Exception as e:
                    log("sync_builds:", e)
            if not self.slot:
                self._track_nodes()
            with self.lock:
                st, desired = self.state, self.desired
            if st == READY and desired:
                why = None
                if self.proc is None or self.proc.poll() is not None:
                    why = self._crash_report()
                else:
                    for nid in self.used:
                        n = self.reg.get(nid)
                        if not n or not self.reg.is_up(n):
                            why = f"nodo {n.name if n else nid} caído"
                            break
                if why:
                    # el estado cambia YA (no cuando arranque el worker): nadie debe ver READY con un nodo caído
                    self._set(RECOVERING)
                    self.recoveries += 1
                    self.event(f"{why}: replanificando con los nodos disponibles")
                    self._start_worker(desired, True)
            elif st == ERROR and desired and self._nodesig() != self.last_nodeset:
                self.event("cambió el conjunto de nodos: reintentando carga")
                self._start_worker(desired, False)

    def _track_nodes(self):
        """Registra altas, caídas y vueltas de los nodos."""
        for n in self.reg.all():
            up, prev = self.reg.is_up(n), self._node_up.get(n.id)
            self._node_up[n.id] = up
            if prev is None:
                if up:
                    self.event(f"nodo {n.name} detectado ({n.host})", node=n.name, kind="node")
            elif prev and not up:
                self.event(f"nodo {n.name} caído: {n.last_error or 'sin respuesta'}", level="warn", node=n.name,
                           kind="node")
            elif up and not prev:
                self.event(f"nodo {n.name} de nuevo en línea", node=n.name, kind="node")

    def _crash_report(self):
        """Motivo de la caída de llama-server: código de salida y líneas relevantes de su log, que se borra al relanzar."""
        rc = self.proc.poll() if self.proc else None
        tail = [l for l in list(self.logbuf)[-60:] if l.strip()]
        key = [l for l in tail if re.search(r"assert|error|abort|fail|out of memory|oom|exception|signal", l, re.I)]
        # las líneas que parecen error y, siempre, las últimas de la salida (el motivo real suele ser lo último)
        pick = key[-4:] + [l for l in tail[-4:] if l not in key[-4:]]
        lines = [l[:300] for l in pick]
        for l in lines:
            self.event(f"  llama-server: {l}")
        self.last_crash = {"t": time.time(), "code": rc, "lines": lines, "rpc": []}
        # si en un nodo murió un rpc-server hace un momento, esa es la causa de fondo (llama-server solo se entera)
        for n in self.reg.all():
            try:
                info = call_agent(self.reg.secret, n, "GET", "/info", timeout=2.5)
            except Exception:
                continue
            ex = info.get("rpc_last_exit")
            if ex and time.time() - ex.get("t", 0) < 120:
                self.event(f"  rpc-server de {n.name} terminó (código {ex.get('code')}){': ' + ex['why'] if ex.get('why') else ''}")
                for l in ex.get("lines", []):
                    self.event(f"    rpc-server: {l}")
                self.last_crash["rpc"].append({"node": n.name, **{k: ex.get(k) for k in ("code", "why", "lines")}})
        return "llama-server terminó" + (f" (código {rc})" if rc is not None else "")

    def _start_worker(self, desired, recovering):
        try:
            path = self.resolve_model(desired[0])
        except ValueError:
            return
        with self.lock:
            self.gen += 1
            gen = self.gen
        threading.Thread(target=self._load_worker, args=(path, desired[0], desired[1], gen, recovering),
                         daemon=True).start()

    # ---------------------------------------------------------- límites de los nodos
    def set_node_limits(self, req):
        n = self.reg.get(req.get("node") or "")
        if not n:
            raise ValueError("nodo desconocido")
        body = {k: req.get(k) for k in ("share", "mem_gb", "threads", "reset", "device") if k in req}
        try:
            r = call_agent(self.reg.secret, n, "POST", "/limits", body, timeout=5)
        except AgentError as e:
            if e.status in (400, 403):  # validación o nodo en modo manual: mensaje para el usuario
                raise ValueError(str(e).split(f"HTTP {e.status} ", 1)[-1])
            raise
        self.reg.probe(n)  # refresca el panel al momento
        in_use = n.id in self.used
        self.event(f"límites de {n.name}: {r['cap_bytes'] / hw.GiB:.1f} GiB · {r['threads']} hilos"
                   + (" (se aplican al recargar el modelo)" if in_use else ""))
        return {"ok": True, "cap_bytes": r["cap_bytes"], "threads": r["threads"], "in_use": in_use}

    # ---------------------------------------------------------- estado / métricas
    # ------------------------------------------------------------ emparejamiento de nodos sin código
    def pending_view(self):
        now = time.time()
        with self.reg.lock:
            for ip in [ip for ip, p in self.reg.pending.items() if now - p["t"] > 30]:
                self.reg.pending.pop(ip, None)
            pend = [{"host": p["host"], "name": p["name"], "http_port": p["http_port"]}
                    for p in self.reg.pending.values()]
        claims = [{k: c[k] for k in ("host", "name", "sas", "state", "detail")} for c in self.claims.values()
                  if now - c["t"] < 600]
        return {"pending": pend, "claims": claims}

    def claim(self, host, port=None):
        """Reclama un nodo sin emparejar: intercambio de claves, código de verificación y espera a que se acepte
        en el nodo; después le envía el secreto cifrado. Devuelve el código de verificación a mostrar.
        Con `port` se puede reclamar por IP un nodo cuyo anuncio no ha llegado."""
        with self.reg.lock:
            p = self.reg.pending.get(host)
        if not p and port:
            p = {"host": host, "name": host, "http_port": int(port)}
        if not p:
            raise ValueError("ese equipo ya no está esperando unirse (¿se ha apagado o ya está emparejado?)")
        x, pub = pairing.keypair()
        c = http.client.HTTPConnection(host, p["http_port"], timeout=8)
        try:
            body = json.dumps({"orch": socket.gethostname(), "pub": str(pub)})
            c.request("POST", "/pair/request", body=body, headers={"Content-Type": "application/json"})
            r = c.getresponse()
            data = json.loads(r.read() or b"{}")
            if r.status != 200:
                raise ValueError(data.get("error") or f"HTTP {r.status}")
        finally:
            c.close()
        key = pairing.shared_key(x, data["pub"])
        code = pairing.sas(key, str(pub), data["pub"])
        cl = {"host": host, "port": p["http_port"], "name": p["name"], "id": data["id"], "key": key, "sas": code,
              "state": "esperando confirmación en el nodo", "detail": "", "t": time.time()}
        self.claims[host] = cl
        self.event(f"reclamando {p['name']} ({host}): código de verificación {code}")
        threading.Thread(target=self._claim_worker, args=(cl,), daemon=True).start()
        return {"sas": code, "name": p["name"]}

    def _orch_ip_for(self, host):
        if self.cfg_advertise():
            return self.cfg_advertise()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 9))  # no envía nada: solo elige la interfaz por la que se llega al nodo
            return s.getsockname()[0]
        finally:
            s.close()

    def join_code(self):
        """Código DLLM1 para que un agente se una (lleva el secreto del clúster: tratarlo como una contraseña)."""
        disk = config.load() or {}
        return config.encode_join({"secret": self.reg.secret, "orch": self.cfg_advertise() or config.lan_ip(),
                                   "beacon": self.cfg.beacon_port, "tag": disk.get("llama_tag"),
                                   "name": disk.get("name") or socket.gethostname()})

    def cfg_advertise(self):
        return getattr(self.cfg, "advertise_host", None)

    def _claim_worker(self, cl):
        def get(path, body=None):
            c = http.client.HTTPConnection(cl["host"], cl["port"], timeout=8)
            try:
                c.request("POST" if body is not None else "GET", path,
                          body=json.dumps(body) if body is not None else None,
                          headers={"Content-Type": "application/json"})
                r = c.getresponse()
                return r.status, json.loads(r.read() or b"{}")
            finally:
                c.close()

        deadline = cl["t"] + pairing.REQUEST_TTL
        try:
            while time.time() < deadline:
                st, d = get(f"/pair/status?id={cl['id']}")
                state = d.get("state")
                if state == "aceptada":
                    box = pairing.seal(cl["key"], {
                        "secret": self.reg.secret, "orch_ip": self._orch_ip_for(cl["host"]),
                        "beacon_port": self.cfg.beacon_port, "orch_name": socket.gethostname(),
                        "tag": installer.tag_from_build(self.llama_build)})
                    st, d = get("/pair/complete", {"id": cl["id"], "box": box})
                    if st != 200:
                        raise RuntimeError(d.get("error") or f"HTTP {st}")
                    cl["state"] = "unido"
                    self.remember_node(cl["host"], cl.get("port") or d.get("port") or 8765)
                    self.event(f"{cl['name']} ({cl['host']}) se ha unido al clúster")
                    return
                if state in ("rechazada", "caducada", "desconocida"):
                    cl["state"] = {"rechazada": "rechazado en el nodo"}.get(state, "caducado")
                    self.event(f"{cl['name']}: emparejamiento {cl['state']}")
                    return
                time.sleep(1.5)
            cl["state"] = "caducado"
        except Exception as e:
            cl["state"], cl["detail"] = "error", str(e)[:200]
            self.event(f"{cl['name']}: no se pudo completar el emparejamiento ({e})")

    # ------------------------------------------------------------ alta por IP, búsqueda, medición y baja de nodos
    def add_node(self, host, port=None):
        """Añade un nodo por IP/nombre cuando el anuncio por broadcast no llega. Si ya habla con nuestro secreto se
        registra tal cual; si está libre se inicia el emparejamiento (código de verificación)."""
        host, port = discovery.valid_host(host), discovery.valid_port(port)
        if self.reg.get(f"{host}:{port}"):
            raise ValueError("ese nodo ya está en la lista")
        self.reg.banned.pop(host, None)
        try:
            info = call_agent(self.reg.secret, Node(host, port), "GET", "/info", timeout=3)
        except Exception:
            info = None
        if info is not None:
            n = self.reg.add(host, port, static=True)
            n.info, n.last_seen, n.last_error = info, time.time(), ""
            self.remember_node(host, port)
            self.event(f"nodo {n.name} añadido a mano ({host}:{port})", node=n.name, kind="node")
            return {"state": "añadido", "name": n.name}
        r = discovery.probe(host, port, 2.0)
        if r is None:
            raise ValueError(f"no responde ningún agente DLLM en {host}:{port}: comprueba que está arrancado, la IP, "
                             "el puerto y que el cortafuegos lo permite")
        if r["state"] == "emparejado":
            raise ValueError("ese agente ya está emparejado con otro secreto (otro orquestador o un clúster anterior): "
                             "reinstálalo sin código de unión o desinstálalo y vuelve a instalarlo")
        self.event(f"{host}:{port}: agente libre; iniciando emparejamiento", kind="node")
        return dict(self.claim(host, port), state="reclamando")

    # -- nodos añadidos por IP: se recuerdan entre reinicios (en otra VLAN no se anunciarían solos)
    @staticmethod
    def _added_file():
        return config.home() / "nodes_added.json"

    def added_nodes(self):
        try:
            v = json.loads(self._added_file().read_text())
            return [x for x in v if isinstance(x, dict) and x.get("host")]
        except (OSError, ValueError):
            return []

    def remember_node(self, host, port, add=True):
        cur = [x for x in self.added_nodes() if (x["host"], int(x.get("port") or 8765)) != (host, int(port))]
        if add:
            cur.append({"host": host, "port": int(port)})
        try:
            self._added_file().parent.mkdir(parents=True, exist_ok=True)
            self._added_file().write_text(json.dumps(cur, indent=2))
        except OSError as e:
            self.event(f"no se pudo guardar la lista de nodos añadidos: {e}", level="warn")

    # ------------------------------------------------------------ copia de seguridad / restauración
    def inventory(self):
        """Modelos instalados (para saber cuáles faltan tras restaurar y poder descargarlos de nuevo)."""
        out = []
        for m in self.models.list():
            parts = m["name"].split("/")
            hf = {"repo": parts[1].replace("__", "/"), "file": parts[-1]} if parts[0] == "hf" and len(parts) >= 3 else None
            out.append({"name": m["name"], "size": m.get("size"), "source": m.get("source"), "quant": m.get("quant"),
                        "hf": hf})
        return out

    def export_backup(self, passphrase, plain=False, include_logs=False):
        if not plain and len(passphrase or "") < 8:
            raise ValueError("indica una contraseña de al menos 8 caracteres (o confirma que la quieres sin cifrar)")
        blob = backup.build(config.home(), self.inventory(), __version__, socket.gethostname(), include_logs,
                            None if plain else passphrase)
        self.event("copia de seguridad exportada" + (" SIN cifrar" if plain else " (cifrada)"),
                   level="warn" if plain else "info", kind="backup")
        return blob, backup.filename(socket.gethostname())

    @staticmethod
    def _decode_backup(data):
        try:
            return base64.b64decode(data or "", validate=True)
        except ValueError:
            raise ValueError("fichero de copia no válido")

    def backup_preview(self, data, passphrase):
        man, files = backup.open_backup(self._decode_backup(data), passphrase)
        s = backup.summary(man, files, config.home())
        have = {m["name"] for m in self.models.list()}
        s["missing_models"] = [m for m in s["models"] if m.get("name") not in have]
        return s

    def backup_import(self, data, passphrase, confirm=False):
        if confirm is not True:
            raise ValueError("restaurar sustituye la configuración actual: confirma la operación (confirm: true)")
        man, files = backup.open_backup(self._decode_backup(data), passphrase)
        actions, safety = backup.restore(files, config.home())
        for x in json.loads(files.get("nodes_added.json") or b"[]"):
            try:
                self.reg.add(discovery.valid_host(x["host"]), discovery.valid_port(x.get("port")), static=True)
            except (ValueError, KeyError, TypeError):
                pass
        cfgj = json.loads(files.get("config.json") or b"{}")
        new_secret = cfgj.get("secret") or (json.loads(files.get("cluster.json") or b"{}")).get("secret")
        new_key = (cfgj.get("orchestrator") or {}).get("api_key")
        restart = bool((new_secret and new_secret != self.reg.secret) or (new_key and new_key != self.cfg.api_key))
        have = {m["name"] for m in self.models.list()}
        missing = [m for m in man.get("models") or [] if m.get("name") not in have]
        self.event(f"configuración restaurada desde una copia del {time.strftime('%d/%m/%Y %H:%M', time.localtime(man.get('created') or 0))}"
                   f" ({len(actions)} elementos)" + ("; hace falta reiniciar DLLM" if restart else ""), kind="backup")
        return {"ok": True, "actions": actions, "safety_backup": safety, "restart_required": restart,
                "missing_models": missing}

    def redownload_models(self, names):
        """Vuelve a descargar de Hugging Face los modelos del inventario que faltan. Los de Ollama o locales no
        se pueden reconstruir solos: se devuelven como `manual`."""
        have = {m["name"] for m in self.models.list()}
        started, manual, failed = [], [], []
        for it in names or []:
            if not isinstance(it, dict) or it.get("name") in have:
                continue
            hf = it.get("hf")
            if not hf:
                manual.append(it.get("name"))
                continue
            try:
                self.models.add_hf(str(hf["repo"]), str(hf["file"]))
                started.append(it["name"])
            except Exception as e:
                failed.append({"name": it.get("name"), "error": str(e)[:160]})
        return {"started": started, "manual": manual, "failed": failed}

    def start_scan(self, cidr=None, port=None):
        """Busca agentes DLLM en un rango de red (por defecto, el /24 del orquestador). Trabajo en segundo plano."""
        job = self.jobs["scan"]
        if job.get("state") == "running":
            raise Busy("ya hay una búsqueda en curso")
        port = discovery.valid_port(port)
        cidr = (cidr or "").strip() or discovery.default_range(config.lan_ip())
        hosts = discovery.hosts_of(cidr)
        job.clear()
        job.update(state="running", range=cidr, port=port, total=len(hosts), done=0, found=[], t=time.time())

        def prog(n, item):
            with self.reg.lock:
                job["done"] += n
                if item:
                    known = next((x for x in self.reg.all() if x.host == item["host"] and x.http_port == item["port"]), None)
                    if item["state"] == "emparejado":
                        item["state"] = "en_cluster" if known else "otro"
                    item["name"] = known.name if known else (self.reg.pending.get(item["host"]) or {}).get("name")
                    job["found"].append(item)

        def run():
            try:
                discovery.scan(hosts, port, prog)
                job["found"].sort(key=lambda x: tuple(int(o) for o in x["host"].split(".")))
                job["state"] = "done"
            except Exception as e:
                job.update(state="error", error=str(e)[:200])
            n = len(job["found"])
            self.event(f"búsqueda en {cidr}:{port}: {n} agente(s) encontrado(s)", kind="node")
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "range": cidr, "total": len(hosts)}

    def start_bench(self, node_ids=None):
        """Mide el enlace de los nodos: primero de uno en uno (cada uno con todo el ancho) y después todos a la vez,
        como en un trabajo real, para ver cuánto cae cuando comparten el enlace del orquestador."""
        job = self.jobs["bench"]
        if job.get("state") == "running":
            raise Busy("ya hay una medición en curso")
        if node_ids:
            nodes = [self.reg.get(i) for i in node_ids]
            if not all(nodes):
                raise ValueError("nodo desconocido")
        else:
            nodes = [n for n in self.reg.all() if self.reg.is_up(n)]
        nodes = [n for n in nodes if self.reg.is_up(n)]
        if not nodes:
            raise ValueError("no hay nodos conectados que medir")
        job.clear()
        job.update(state="running", phase="uno a uno", total=len(nodes), done=0, current=None, items=[], joint=None,
                   t=time.time(), min_mbps=self.cfg.min_mbps)
        threading.Thread(target=self._bench_worker, args=(nodes, job), daemon=True).start()
        return {"ok": True, "nodes": [n.name for n in nodes]}

    def _bench_worker(self, nodes, job):
        try:
            for n in nodes:
                job["current"] = n.name
                it = {"node": n.name, "id": n.id}
                try:
                    n.net = bench_node(self.reg.secret, n)
                    it.update(mbps=n.net["mbps"], rtt_ms=n.net["rtt_ms"], tier=n.net.get("tier"),
                              local=n.net.get("local"))
                except Exception as e:
                    n.net, n.last_error = None, f"bench: {e}"[:200]
                    it["error"] = str(e)[:160]
                job["items"].append(it)
                job["done"] += 1
            # conjunto: todos a la vez (los locales no cruzan la red y no cuentan)
            remote = [n for n in nodes if n.net and not n.net.get("local") and n.rpc_port]
            if len(remote) >= 2:
                job["phase"], job["current"] = "en conjunto", None
                res = {}

                def one(n):
                    try:
                        res[n.id] = probe_link(n.host, n.rpc_port, self.reg.secret)
                    except Exception as e:
                        res[n.id] = {"error": str(e)[:120]}
                ths = [threading.Thread(target=one, args=(n,), daemon=True) for n in remote]
                for t in ths:
                    t.start()
                for t in ths:
                    t.join(timeout=60)
                rows = []
                for n in remote:
                    r = res.get(n.id) or {}
                    if "mbps" in r:
                        n.net["joint_mbps"] = r["mbps"]
                        n.net["joint_ratio"] = round(r["mbps"] / max(1, n.net["mbps"]), 2)
                        rows.append({"node": n.name, "alone": n.net["mbps"], "joint": r["mbps"],
                                     "ratio": n.net["joint_ratio"]})
                job["joint"] = {"nodes": rows, "total_mbps": sum(x["joint"] for x in rows)}
            for it in job["items"]:
                n = self.reg.get(it["id"])
                ns = self.node_settings(n.name) if n else {}
                it["slow"] = bool(it.get("mbps") is not None and it["mbps"] < self.cfg.min_mbps and not it.get("local"))
                it["allow_slow"] = bool(ns.get("allow_slow"))
                if it["slow"]:
                    self.event(f"{it['node']}: red de {it['mbps']} Mbps, por debajo del mínimo de "
                               f"{self.cfg.min_mbps:g} Mbps; no se usará salvo que lo permitas (modo forzado)",
                               level="warn", node=it["node"], kind="net")
                elif it.get("error"):
                    self.event(f"{it['node']}: no se pudo medir la red ({it['error']})", level="warn", node=it["node"],
                               kind="net")
            for x in (job["joint"] or {}).get("nodes", []):
                if x["ratio"] < 0.6:
                    self.event(f"{x['node']}: con todos los nodos a la vez baja de {x['alone']} a {x['joint']} Mbps: "
                               "el enlace del orquestador se reparte entre ellos", level="warn", node=x["node"],
                               kind="net")
            self.event("medición de red terminada: " + ", ".join(
                f"{i['node']} {i['mbps']} Mbps" if "mbps" in i else f"{i['node']} sin medir" for i in job["items"]),
                kind="net")
            job.update(state="done", phase="terminado", current=None)
        except Exception as e:
            job.update(state="error", error=str(e)[:200], current=None)

    def forget_node(self, nid, block=True):
        """Elimina un nodo de la lista (p. ej. con una IP vieja). Si está en uso, antes hay que descargar el modelo.
        Si sigue anunciándose, se ignoran sus anuncios 10 min para que no reaparezca al instante."""
        n = self.reg.get(nid or "")
        if not n:
            raise ValueError("nodo desconocido")
        if any(n.id in c.used for c in list(self.slots.values())):
            raise Busy(f"{n.name} está en uso por un modelo: descárgalo antes de eliminar el nodo")
        up = self.reg.is_up(n)
        self.reg.remove(nid, 600 if block and up and not n.static else 0)
        self._node_up.pop(nid, None)
        self.claims.pop(n.host, None)
        self.remember_node(n.host, n.http_port, add=False)
        if not any(x.name == n.name for x in self.reg.all()):
            allns = self.all_node_settings()
            if allns.pop(n.name, None) is not None:
                self._node_file().write_text(json.dumps(allns, indent=2, ensure_ascii=False))
        self.event(f"nodo {n.name} ({n.host}) eliminado de la lista", kind="node")
        return {"ok": True, "static": n.static, "was_up": up,
                "note": ("sigue vivo y se anuncia: se ignora 10 min; para quitarlo del todo, desinstálalo en ese equipo"
                         if up and not n.static else "")}

    def node_models(self, n):
        """Modelos que tienen reparto en el nodo."""
        out = []
        for k, c in sorted(self.slots.items()):
            if not c.plan or c.state in (IDLE, ERROR):
                continue
            for a in c.plan.get("assignments") or []:
                if a.get("node_id") == n.id:
                    out.append({"slot": k, "model": c.model, "state": c.state, "device": a.get("device"),
                                "bytes": a.get("bytes"), "layers": a.get("layers")})
        return out

    def snapshot(self):
        nodes = []
        for n in self.reg.all():
            i = n.info
            nodes.append({"id": n.id, "name": n.name, "host": n.host, "up": self.reg.is_up(n),
                          "backend": i.get("backend"), "cap_bytes": i.get("cap_bytes"),
                          "bw_gbps": i.get("bw_gbps"), "cpu": i.get("cpu"), "os": i.get("os"),
                          "ram_free": i.get("ram_free"), "gpu": i.get("gpu"), "net": n.net,
                          "rpc_bin": i.get("rpc_bin"), "rpc_build": i.get("rpc_build"),
                          "device": i.get("device"), "device_desc": i.get("device_desc"), "gpu_warning": i.get("gpu_warning"),
                          "devices": i.get("devices"), "update": i.get("update"),
                          "auto_update": i.get("auto_update"), "settings": self.node_settings(n.name),
                          "telemetry": i.get("telemetry"),
                          "rpc_running": i.get("rpc_running"), "models": self.node_models(n), "static": n.static,
                          "used": any(n.id in c.used for c in list(self.slots.values())),
                          "error": n.last_error, "cores": i.get("cores"), "threads": i.get("threads"),
                          "ram_total": i.get("ram_total"), "limits_mode": i.get("limits_mode"),
                          "limits": i.get("limits"), "mem_source": i.get("mem_source"),
                          "threads_source": i.get("threads_source"), "recommended": i.get("recommended"),
                          "ceiling": i.get("ceiling")})
        return {"version": __version__, "notice": legal_notice(), "multimodal": bool(self.mmproj and self.state == READY),
                "mmproj": self.mmproj, "state": self.state, "error": self.error, "model": self.model,
                "ctx": self.ctx, "plan": self.plan, "recoveries": self.recoveries, "nodes": nodes,
                "slots": self.slots_view(),
                "ledger": self.ledger.view({n.id: n.info.get("cap_bytes") or 0 for n in self.reg.all()}),
                "events": list(self.events)[-40:], "event_counts": self.elog.counts(), "llama_log": list(self.logbuf)[-15:], "last_crash": self.last_crash,
                "llama_build": self.llama_build, "min_mbps": self.cfg.min_mbps, "jobs": {k: dict(v) for k, v in self.jobs.items()},
                "scan_default": discovery.default_range(config.lan_ip()), **self.pending_view()}

    def metrics(self):
        L = ["# HELP dllm_state Estado del orquestador (1 = estado actual)", "# TYPE dllm_state gauge"]
        for s in (IDLE, LOADING, READY, RECOVERING, ERROR):
            L.append(f'dllm_state{{state="{s}"}} {int(self.state == s)}')
        L += ["# TYPE dllm_node_up gauge", "# TYPE dllm_node_cap_bytes gauge", "# TYPE dllm_node_net_mbps gauge"]
        for n in self.reg.all():
            lab = f'node="{n.name}"'
            L.append(f"dllm_node_up{{{lab}}} {int(self.reg.is_up(n))}")
            L.append(f"dllm_node_cap_bytes{{{lab}}} {n.info.get('cap_bytes') or 0}")
            L.append(f"dllm_node_net_mbps{{{lab}}} {(n.net or {}).get('mbps', 0)}")
        L += ["# TYPE dllm_recoveries_total counter", f"dllm_recoveries_total {self.recoveries}",
              "# TYPE dllm_requests_total counter", f"dllm_requests_total {self.requests}",
              "# TYPE dllm_request_errors_total counter", f"dllm_request_errors_total {self.req_errors}"]
        # por modelo (slot) y por nodo
        L += ["# TYPE dllm_slot_requests_total counter", "# TYPE dllm_slot_errors_total counter",
              "# TYPE dllm_slot_latency_p95_ms gauge", "# TYPE dllm_slot_tokens_per_second gauge",
              "# TYPE dllm_slot_active_requests gauge", "# TYPE dllm_slot_recoveries_total counter"]
        for k, c in sorted(self.slots.items()):
            lab = f'slot="{k}",model="{(c.model or "").replace(chr(34), "")}"'
            sm = c.metrics_req.summary()
            L.append(f"dllm_slot_requests_total{{{lab}}} {sm['requests']}")
            L.append(f"dllm_slot_errors_total{{{lab}}} {sm['errors']}")
            L.append(f"dllm_slot_active_requests{{{lab}}} {c.inflight}")
            L.append(f"dllm_slot_recoveries_total{{{lab}}} {c.recoveries}")
            if sm["p95_ms"] is not None:
                L.append(f"dllm_slot_latency_p95_ms{{{lab}}} {sm['p95_ms']}")
            tk = self._live(c)["tok_s"]
            if tk is not None:
                L.append(f"dllm_slot_tokens_per_second{{{lab}}} {tk}")
        L += ["# TYPE dllm_node_rpc_restarts_total counter", "# TYPE dllm_node_ram_free_bytes gauge",
              "# TYPE dllm_node_vram_reserved_bytes gauge", "# TYPE dllm_events_total counter"]
        for n in self.reg.all():
            lab = f'node="{n.name}"'
            L.append(f"dllm_node_rpc_restarts_total{{{lab}}} {n.info.get('rpc_restarts') or 0}")
            L.append(f"dllm_node_ram_free_bytes{{{lab}}} {n.info.get('ram_free') or 0}")
            L.append(f"dllm_node_vram_reserved_bytes{{{lab}}} {self.ledger.reserved(n.id, 'vram')}")
        for lv, cnt in self.elog.counts().items():
            L.append(f'dllm_events_total{{level="{lv}"}} {cnt}')
        return "\n".join(L) + "\n"

    def shutdown(self):
        for c in [c for k, c in list(self.slots.items()) if k and not self.parent]:
            c.dead = True
            c.shutdown()
        with self.lock:
            self.gen += 1
            self.desired = None
        self._teardown()


# ============================================================ HTTP
def extract_model(body, ctype=""):
    """Campo «model» de la petición: JSON o formulario multipart (p. ej. /v1/audio/transcriptions)."""
    if not body:
        return None
    try:
        if "multipart/form-data" in (ctype or ""):
            m = re.search(rb'name="model"\r\n\r\n([^\r\n]{1,200})', body[:1 << 20])
            return m.group(1).decode("utf-8", "replace") if m else None
        d = json.loads(body)
        v = d.get("model") if isinstance(d, dict) else None
        return v if isinstance(v, str) else None
    except ValueError:
        return None


def make_handler(cfg, cl):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dllm"

        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, raw=None, ctype="application/json", headers=None, gz=None):
            if raw is None and code >= 400:
                obj = i18n.tr_error(obj, i18n.request_lang(self.headers, urlparse(self.path).query))
            body = raw if raw is not None else json.dumps(obj, separators=(",", ":")).encode()
            # panel y API de gestión: gzip si el cliente lo acepta y compensa (la API /v1 va tal cual, sin latencia extra)
            if gz is None and len(body) >= 1400 and not self.path.startswith("/v1") \
                    and (ctype.startswith("text/") or ctype.startswith("application/json")) \
                    and "gzip" in self.headers.get("Accept-Encoding", ""):
                gz = gzip.compress(body, 5)
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, str(v))
            if gz is not None and "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gz
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            """Sin clave desde este mismo equipo (el navegador del orquestador); desde la red, API key."""
            if not cfg.api_key or self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return True
            return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + cfg.api_key)

        def _same_origin(self):
            """Contra CSRF / DNS rebinding: una web ajena abierta en el navegador no puede mandar órdenes al panel."""
            origin = self.headers.get("Origin")
            if not origin:  # curl, scripts, clientes de la API
                return True
            return urlparse(origin).netloc == self.headers.get("Host", "")

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def _route(self):
            path = urlparse(self.path).path
            try:
                if path == "/":
                    html, gz, etag = _ui_page()
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.send_header("ETag", etag)
                        self.end_headers()
                        return
                    return self._send(200, raw=html, gz=gz, ctype="text/html; charset=utf-8",
                                      headers={"ETag": etag, "Cache-Control": "no-cache"})
                if path == "/healthz":
                    return self._send(200, {"ok": True})
                ip = self.client_address[0]
                if cfg.allow_ip and ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and ip not in cfg.allow_ip:
                    self.close_connection = True
                    return self._send(403, {"error": "IP no permitida"})
                if self.command == "POST" and not self._same_origin():
                    return self._send(403, {"error": "origen no permitido"})
                if not self._authed():
                    self.close_connection = True
                    return self._send(401, {"error": "API key requerida (Authorization: Bearer ...)"})
                body = b""
                if self.command == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > (24 << 20) and not path.startswith("/v1/"):
                        self.close_connection = True
                        return self._send(413, {"error": "petición demasiado grande"})
                    body = self.rfile.read(n) if n else b""
                if path.startswith("/v1/"):
                    return self._proxy(body)
                req = json.loads(body) if body else {}
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                mm = cl.models
                if path in ("/api/ui/ping", "/api/ui/bye") and self.command == "POST":
                    cid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                    (cl.presence.touch if path.endswith("ping") else cl.presence.bye)(cid)
                    return self._send(200, {"ok": True})
                if path == "/api/join-code":
                    # lleva el secreto del clúster: la API key de los front-ends no basta, solo desde este equipo
                    if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                        return self._send(403, {"error": "el código de unión solo se muestra en el propio orquestador "
                                                         "(abre el panel allí o usa `dllm join-code`)"})
                    cl.event("código de unión consultado desde " + self.client_address[0], kind="security")
                    return self._send(200, {"code": cl.join_code(), "orch": cl.cfg_advertise() or config.lan_ip(),
                                            "beacon": cfg.beacon_port}, headers={"Cache-Control": "no-store"})
                if path == "/api/ui/clients":
                    return self._send(200, cl.presence.view())
                if path == "/api/pair/claim" and self.command == "POST":
                    try:
                        return self._send(200, cl.claim(str(req.get("host") or ""), req.get("port") and discovery.valid_port(req.get("port"))))
                    except (ValueError, OSError) as e:
                        return self._send(400, {"error": str(e)})
                if path == "/api/access":
                    local = self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                    out = {"local": local, "key_required": bool(cfg.api_key) and not local,
                           "ollama_port": getattr(cfg, "ollama_port", 0),
                           "exposed": cfg.host not in ("127.0.0.1", "localhost", "::1")}
                    if local and cfg.api_key:
                        out["lan_url"] = f"http://{config.lan_ip()}:{cfg.port}/#key={cfg.api_key}"
                    return self._send(200, out)
                if path == "/api/state":
                    return self._send(200, cl.snapshot())
                if path == "/api/models":
                    return self._send(200, cl.list_models())
                if path == "/api/events":
                    sl = q.get("slot")
                    return self._send(200, {"events": cl.elog.query(
                        q.get("level"), int(sl) if sl not in (None, "") else None, q.get("node") or None,
                        q.get("q"), float(q["since"]) if q.get("since") else None, int(q.get("limit") or 300)),
                        "counts": cl.elog.counts()})
                if path == "/metrics":
                    return self._send(200, raw=cl.metrics().encode(), ctype="text/plain; version=0.0.4")
                if path == "/api/resources":
                    return self._send(200, cl.resources())
                if path == "/api/plan" and self.command == "POST":
                    # simula sin cargar: el reparto, y los recursos del clúster antes y después
                    slot = req.get("slot", 0)
                    owner = cl.slots.get(int(slot)) if str(slot).isdigit() else None
                    # «new»: modelo nuevo, no sustituye a ninguno (se ve con la memoria que ya ocupan los demás)
                    _, p = (owner or cl).make_plan(cl.resolve_model(req["model"]), int(req.get("ctx", 4096)),
                                                   force_bench=False, targets=req.get("targets") or [],
                                                   own=owner is not None)
                    p["resources"] = cl.resources(p.get("reserve") if p.get("ok") else None,
                                                  skip={owner.slot} if owner is not None else ())
                    return self._send(200, p)
                if path == "/api/load" and self.command == "POST":
                    ctx = req.get("ctx") or mm.settings(req["model"]).get("ctx") or 4096
                    slot = cl.load_model(req["model"], int(ctx), req.get("slot", 0), req.get("pinned", True),
                                         req.get("targets") or None)
                    return self._send(202, {"accepted": True, "slot": slot})
                # ---- gestión de modelos
                if path == "/api/hf/search":
                    return self._send(200, models.hf_search(q.get("q"), mm.hf_token()))
                if path == "/api/hf/files":
                    return self._send(200, models.hf_files(q.get("repo"), mm.hf_token()))
                if path == "/api/hf/token":
                    if self.command == "POST":
                        mm.set_hf_token(req.get("token"))
                    return self._send(200, {"set": bool(mm.hf_token())})
                if path == "/api/ollama/local":
                    return self._send(200, {"dir": str(models.ollama_local_dir()), "models": models.ollama_local_list()})
                if path == "/api/downloads":
                    if self.command == "POST":
                        src = req.get("source")
                        if src == "hf":
                            job = mm.add_hf(req.get("repo"), req.get("file"))
                        elif src == "ollama":
                            job = mm.add_ollama(req.get("name"))
                        elif src == "ollama-local":
                            job = mm.add_ollama_local(req.get("name"))
                        else:
                            raise ValueError("origen desconocido (hf, ollama u ollama-local)")
                        cl.event(f"descarga en cola: {job['label']}")
                        return self._send(202, job)
                    return self._send(200, mm.views())
                if path == "/api/downloads/cancel" and self.command == "POST":
                    return self._send(200, {"ok": mm.cancel(req.get("id"))})
                if path == "/api/downloads/clear" and self.command == "POST":
                    return self._send(200, {"removed": mm.clear(req.get("id"))})
                if path == "/api/models/delete" and self.command == "POST":
                    cl.delete_model(req.get("model"))
                    return self._send(200, {"ok": True})
                if path == "/api/models/settings" and self.command == "POST":
                    return self._send(200, {"settings": mm.set_settings(req.get("model"), req.get("settings") or {})})
                if path == "/api/node/settings" and self.command == "POST":
                    return self._send(200, cl.set_node_settings(req))
                if path == "/api/telemetry":
                    q = parse_qs(urlparse(self.path).query).get("since", [None])[0]
                    try:
                        since = float(q) if q is not None else None
                    except ValueError:
                        raise ValueError("'since' debe ser una marca de tiempo")
                    return self._send(200, cl.telemetry(since))
                if path == "/api/backup/export" and self.command == "POST":
                    blob, fname = cl.export_backup(req.get("passphrase"), req.get("plain") is True,
                                                   req.get("include_logs") is True)
                    return self._send(200, raw=blob, ctype="application/octet-stream",
                                      headers={"Content-Disposition": f'attachment; filename="{fname}"',
                                               "X-Filename": fname, "Cache-Control": "no-store"})
                if path == "/api/backup/preview" and self.command == "POST":
                    return self._send(200, cl.backup_preview(req.get("data"), req.get("passphrase")))
                if path == "/api/backup/import" and self.command == "POST":
                    return self._send(200, cl.backup_import(req.get("data"), req.get("passphrase"),
                                                            req.get("confirm")))
                if path == "/api/backup/redownload" and self.command == "POST":
                    return self._send(200, cl.redownload_models(req.get("models")))
                if path == "/api/tools/test" and self.command == "POST":
                    return self._send(200, cl.test_tools(req.get("slot", 0)))
                if path == "/api/nodes/add" and self.command == "POST":
                    return self._send(200, cl.add_node(req.get("host"), req.get("port")))
                if path == "/api/nodes/scan":
                    if self.command == "POST":
                        return self._send(200, cl.start_scan(req.get("range"), req.get("port")))
                    return self._send(200, cl.jobs["scan"])
                if path == "/api/nodes/bench":
                    if self.command == "POST":
                        return self._send(200, cl.start_bench(req.get("nodes") or None))
                    return self._send(200, cl.jobs["bench"])
                if path == "/api/nodes/forget" and self.command == "POST":
                    return self._send(200, cl.forget_node(req.get("node"), req.get("block", True)))
                if path == "/api/node/limits" and self.command == "POST":
                    return self._send(200, cl.set_node_limits(req))
                if path == "/api/unload" and self.command == "POST":
                    if req.get("slot") in (None, 0, "0"):
                        cl.unload()
                    else:
                        cl.drop_slot(req["slot"])
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "no encontrado"})
            except Busy as e:
                self._send(409, {"error": str(e)})
            except (ValueError, KeyError, models.ModelError) as e:
                self._send(400, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log("error en", self.path, e)
                self._send(500, {"error": str(e)})

        def _proxy(self, body):
            """Enrutador L7: elige el slot por el campo «model» y la capacidad que exige la ruta."""
            cl.requests += 1
            path = urlparse(self.path).path
            if path == "/v1/models" and self.command == "GET":
                return self._send(200, cl.models_listing())
            try:
                c = cl.route(extract_model(body, self.headers.get("Content-Type", "")), engine.capability_of(path))
            except RouteError as e:
                cl.req_errors += 1
                cl.event(f"petición rechazada ({e.status}): {e}", level="warn", kind="route")
                return self._send(e.status, oai_error(e.status, str(e)),
                                  headers={"Retry-After": e.retry_after} if e.retry_after else None)
            with c.lock:
                c.inflight += 1
            c.last_used = time.time()
            t0, self._st = time.time(), (0, "")
            try:
                self._forward(c, path, body)
            finally:
                with c.lock:
                    c.inflight -= 1
                c.last_used = time.time()
                status, err = self._st
                if status:
                    c.metrics_req.record(status, time.time() - t0, err)
                    if status >= 500:
                        cl.event(f"petición {path} → {status}: {err or 'error del modelo'}", level="error",
                                 node=None, kind="request", detail={"slot": c.slot, "model": c.model, "path": path})

        def _forward(self, c, path, body):
            if body and path in ("/v1/chat/completions", "/v1/completions"):
                # parámetros por defecto del modelo (temperatura, prompt de sistema…) sin pisar los del cliente
                try:
                    st = cl.models.settings(c.model) if c.model else {}
                    body = json.dumps(models.apply_settings(path, json.loads(body), st)).encode()
                except ValueError:
                    pass  # cuerpo no JSON: que llama-server responda el error
            up = http.client.HTTPConnection("127.0.0.1", c.llama_port, timeout=3600)
            try:
                up.request(self.command, self.path, body=body or None,
                           headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
                r = up.getresponse()
            except OSError as e:
                cl.req_errors += 1
                up.close()
                self._st = (502, f"llama-server no responde: {e}")
                return self._send(502, oai_error(502, f"llama-server no responde: {e}"))
            try:
                ctype = r.getheader("Content-Type", "application/json")
                self._st = (r.status, "" if r.status < 500 else f"HTTP {r.status} de llama-server")
                if r.getheader("Content-Length") is not None:
                    return self._send(r.status, raw=r.read(), ctype=ctype)
                # streaming (SSE): reenvío troceado
                self.send_response(r.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while True:
                    chunk = r.read1(8192)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            finally:
                if r.status >= 500:
                    cl.req_errors += 1
                up.close()

    return H


_UI = []


def _ui_page():
    """Panel (90 KB) leído, comprimido y con ETag una sola vez; el navegador lo revalida con 304."""
    if not _UI:
        html = i18n.inject(pkgutil.get_data("dllm", "ui.html").decode("utf-8")).encode("utf-8")  # también en .pyz
        _UI.extend([html, gzip.compress(html, 9), '"%s"' % hashlib.sha256(html).hexdigest()[:16]])
    return _UI


def run(cfg):
    secret = cfg.secret
    if not secret:
        sys.exit("falta el secreto del clúster: ejecuta `dllm init` o usa --secret / DLLM_SECRET")
    loopback = cfg.host in ("127.0.0.1", "localhost", "::1")
    if cfg.api_key == "auto":
        cfg.api_key = auth.new_secret()[:24]
        log(f"API key generada: {cfg.api_key}")
    if not loopback and not cfg.api_key:
        sys.exit("escuchar fuera de loopback exige --api-key (o --api-key auto)")
    if not cfg.llama_bin:
        log("AVISO: llama-server no encontrado; podrás ver nodos pero no cargar modelos")

    cfg.events_file = str(config.home() / "events.jsonl")  # registro persistente (rota por tamaño)
    reg = Registry(secret, cfg.node_timeout, cfg.probe_interval)
    for n in cfg.node or []:
        h, _, p = n.partition(":")
        reg.add(h, int(p or 8765), static=True)
    cl = Cluster(cfg, reg)
    for x in cl.added_nodes():  # nodos añadidos a mano en otras sesiones
        try:
            reg.add(discovery.valid_host(x["host"]), discovery.valid_port(x.get("port")), static=True)
        except ValueError:
            pass
    for fn in (reg.probe_loop, cl.watchdog, cl.metrics_loop):
        threading.Thread(target=fn, daemon=True).start()
    threading.Thread(target=reg.beacon_loop, args=(cfg.beacon_port,), daemon=True).start()

    oport = getattr(cfg, "ollama_port", 0)
    if oport:  # API compatible con Ollama en su propio puerto (mismas reglas: fuera de loopback, API key)
        from . import ollama_api
        try:
            osrv = FastHTTPServer((cfg.host, oport), ollama_api.make_handler(cfg, cl))
            threading.Thread(target=osrv.serve_forever, daemon=True).start()
            log(f"API compatible con Ollama en http://{cfg.host}:{oport}")
        except OSError as e:
            cl.event(f"API compatible con Ollama desactivada: no se puede abrir el puerto {oport} ({e}); "
                     "¿hay un Ollama real usándolo? Usa --ollama-port con otro puerto", level="warn")
    srv = FastHTTPServer((cfg.host, cfg.port), make_handler(cfg, cl))
    srv.daemon_threads = True
    log(legal_notice().replace("\n", " · "))
    log(f"DLLM {__version__} · UI y API en http://{cfg.host}:{cfg.port} · modelos: {cfg.models_dir}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("cerrando: deteniendo llama-server y rpc-servers")
        cl.shutdown()
