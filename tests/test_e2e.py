# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Prueba de extremo a extremo con binarios simulados (rpc-server / llama-server de tests/mockbin).

Levanta 3 agentes con hardware ficticio + el orquestador, carga un modelo de 6 GiB (disperso),
comprueba el reparto, la API OpenAI (normal y streaming), la autenticación, y que el clúster
se recupera solo cuando muere un nodo en uso."""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _proc  # noqa: E402
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402

GiB = 1 << 30
SECRET, KEY = "test-secret", "test-key"
from dllm.config import lan_ip  # noqa: E402
LAN = lan_ip()


@unittest.skipIf(os.name == "nt", "usa grupos de procesos POSIX (killpg) y mocks con shebang")
class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.models = os.path.join(cls.tmp, "models")
        os.makedirs(cls.models)
        write_gguf(os.path.join(cls.models, "mock.gguf"), 3 * GiB)
        write_gguf(os.path.join(cls.models, "mmproj-mock-f16.gguf"), 1 << 20, arch="clip")  # proyector multimodal
        cls.env = dict(os.environ, PYTHONPATH=ROOT, DLLM_HOME=os.path.join(cls.tmp, "home"))
        cls.procs = {}
        cls.op, cls.bp = free_port(), free_port()
        mock = os.path.join(HERE, "mockbin")
        # (nombre, memoria GiB, ancho de banda GB/s, modo): con un modelo de ~3,4 GiB hacen falta los tres.
        # Tamaños pequeños para que quepan en los runners de CI (macOS tiene 7 GB).
        for name, mem, bw, mode in (("n1", 2, 100, "auto"), ("n2", 4, 20, "manual"), ("n3", 1.5, 60, "auto")):
            cls.procs[name] = cls._spawn([
                "agent", "--secret", SECRET, "--name", name, "--port", str(free_port()),
                "--rpc-port", str(free_port()), "--bin-dir", mock, "--backend", "cpu",
                "--mem-gb", str(mem), "--bw-gbps", str(bw), "--announce-to", f"127.0.0.1:{cls.bp}",
                "--beacon-interval", "0.5", "--limits-mode", mode])
        cls.procs["orch"] = cls._spawn([
            "up", "--secret", SECRET, "--models-dir", cls.models, "--port", str(cls.op),
            "--bin-dir", mock, "--beacon-port", str(cls.bp), "--api-key", KEY, "--host", "0.0.0.0",
            "--node-timeout", "3", "--probe-interval", "0.5"])
        try:
            cls.wait(lambda: sum(n["up"] for n in cls.get("/api/state")["nodes"]) == 3, 30, "3 nodos descubiertos")
        except Exception:
            cls.tearDownClass()  # si setUpClass falla, unittest no llama a tearDownClass: no dejar procesos
            raise

    @classmethod
    def _spawn(cls, args):
        log = open(os.path.join(cls.tmp, f"{args[0]}-{len(cls.procs)}.log"), "w")
        return subprocess.Popen([sys.executable, "-m", "dllm"] + args, cwd=ROOT, env=cls.env,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    @classmethod
    def logs(cls):
        out = []
        for f in sorted(os.listdir(cls.tmp)):
            if f.endswith(".log"):
                with open(os.path.join(cls.tmp, f)) as fh:
                    out.append(f"--- {f}\n" + fh.read()[-3000:])
        return "\n".join(out)

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs.values():
            try:
                _proc.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1)
        for p in cls.procs.values():
            try:
                _proc.killpg(p.pid, _proc.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if not getattr(cls, "_cleaned", False):
            cls._cleaned = True
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    @classmethod
    def req(cls, method, path, body=None, key=KEY, port=None, raw=False, remote=False, headers=None):
        # remote: desde la IP de la LAN (como otro equipo); desde 127.0.0.1 el panel no pide clave
        c = http.client.HTTPConnection(LAN if remote else "127.0.0.1", port or cls.op, timeout=60)
        h = dict({"Content-Type": "application/json"}, **(headers or {}))
        if key:
            h["Authorization"] = "Bearer " + key
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (data if raw else json.loads(data or b"{}"))

    @classmethod
    def get(cls, path):
        return cls.req("GET", path)[1]

    @classmethod
    def wait(cls, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return
            except Exception:
                pass
            time.sleep(0.3)
        state = ""
        try:
            state = json.dumps(cls.get("/api/state")["nodes"])[:2000]
        except Exception as e:
            state = f"(sin estado: {e})"
        raise AssertionError(f"timeout esperando: {what}\nnodos: {state}\n{cls.logs()}")

    def state(self):
        return self.get("/api/state")

    # ------------------------------------------------------------ tests (en orden)
    def test_1_security(self):
        self.assertEqual(self.req("GET", "/api/state", key=None)[0], 200)   # en el propio equipo, sin clave
        if LAN.startswith("127."):
            self.skipTest("sin IP de red para simular otro equipo")
        self.assertEqual(self.req("GET", "/api/state", key=None, remote=True)[0], 401)
        self.assertEqual(self.req("GET", "/api/state", key="mala", remote=True)[0], 401)
        self.assertEqual(self.req("GET", "/api/state", remote=True)[0], 200)
        st, a = self.req("GET", "/api/access", key=None)
        self.assertTrue(a["local"] and a["lan_url"].endswith("#key=" + KEY))
        self.assertNotIn("lan_url", self.req("GET", "/api/access", remote=True)[1])
        # una web ajena abierta en el navegador no puede dar órdenes (CSRF)
        st, _ = self.req("POST", "/api/unload", {}, key=None, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        # el agente rechaza peticiones sin firma
        n = self.state()["nodes"][0]
        c = http.client.HTTPConnection(n["host"], 0 or int(n["id"].split(":")[1]), timeout=5)
        c.request("GET", "/info")
        self.assertEqual(c.getresponse().status, 401)

    def test_2_plan(self):
        st, p = self.req("POST", "/api/plan", {"model": "mock.gguf", "ctx": 2048})
        self.assertEqual(st, 200)
        self.assertTrue(p["ok"], p)
        self.assertEqual([a["name"] for a in p["assignments"]], ["n1", "n3", "n2"])  # por ancho de banda
        self.assertEqual(sum(a["layers"] for a in p["assignments"]), 32)
        self.assertGreater(p["max_ctx"], 2048)  # contexto máximo con la memoria de todos los nodos
        self.assertEqual(self.req("POST", "/api/plan", {"model": "../../etc/passwd"})[0], 400)

    def test_3_load_and_serve(self):
        self.assertEqual(self.req("POST", "/api/load", {"model": "mock.gguf", "ctx": 2048})[0], 202)
        self.wait(lambda: self.state()["state"] in ("READY", "ERROR"), 60, "carga")
        s = self.state()
        self.assertEqual(s["state"], "READY", s.get("error"))
        # chat normal: el mock comprueba que CADA endpoint --rpc responde a través del túnel autenticado
        st, r = self.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual(st, 200)
        self.assertEqual(r["mock"]["echo_ok"], [True, True, True])
        # multimodal: el proyector junto al modelo se detecta solo y se ejecuta en el orquestador (sin offload)
        self.assertEqual(len(r["mock"]["device"].split(",")), 3)  # solo los dispositivos remotos, uno por nodo
        self.assertTrue(r["mock"]["mmproj"].endswith("mmproj-mock-f16.gguf"))
        self.assertFalse(r["mock"]["mmproj_offload"])
        self.assertTrue(self.state()["multimodal"])
        self.assertEqual(len(r["mock"]["ts"].split(",")), 3)
        # streaming SSE
        st, raw = self.req("POST", "/v1/chat/completions", {"messages": [], "stream": True}, raw=True)
        self.assertEqual(st, 200)
        self.assertIn(b"tok0", raw)
        self.assertIn(b"[DONE]", raw)
        st, m = self.req("GET", "/v1/models")
        self.assertEqual(m["data"][0]["id"], "mock.gguf")
        # sin API key no se puede usar el endpoint de inferencia
        if not LAN.startswith("127."):
            self.assertEqual(self.req("POST", "/v1/chat/completions", {"messages": []}, key=None, remote=True)[0], 401)

    def test_3b_model_settings_and_management(self):
        st, ms = self.req("GET", "/api/models")
        self.assertEqual([m["name"] for m in ms], ["mock.gguf"])  # el mmproj no aparece como modelo
        self.assertEqual(ms[0]["mmproj"], "mmproj-mock-f16.gguf")
        st, r = self.req("POST", "/api/models/settings", {"model": "mock.gguf", "settings": {
            "temperature": 0.3, "max_tokens": 50, "system_prompt": "Sé breve"}})
        self.assertEqual(st, 200, r)
        st, r = self.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hola"}]})
        got = r["mock"]["received"]
        self.assertEqual((got["temperature"], got["max_tokens"]), (0.3, 50))
        self.assertEqual(got["messages"][0]["role"], "system")
        self.assertTrue(got["messages"][0]["content"].startswith("Sé breve\n\nFecha actual"))
        self.assertIn("(hora del servidor: ", got["messages"][-1]["content"])
        st, r = self.req("POST", "/v1/chat/completions", {"temperature": 1.1, "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r["mock"]["received"]["temperature"], 1.1)  # lo que pide el cliente manda
        self.assertEqual(self.req("POST", "/api/models/settings", {"model": "mock.gguf", "settings": {"temperature": 99}})[0], 400)
        self.assertEqual(self.req("POST", "/api/models/delete", {"model": "mock.gguf"})[0], 409)  # está cargado
        self.assertEqual(self.req("POST", "/api/downloads", {"source": "ftp"})[0], 400)
        self.assertEqual(self.req("POST", "/api/models/settings", {"model": "mock.gguf", "settings": {}})[0], 200)

    def test_4_rpc_port_requires_auth(self):
        """Conectar al puerto RPC público de un agente sin conocer el secreto no da acceso."""
        s = self.state()
        n = next(x for x in s["nodes"] if x["used"])
        info_port = None
        # el puerto RPC público lo da el propio agente en /info; lo pedimos con firma válida
        sys.path.insert(0, ROOT)
        from dllm import auth, orchestrator
        node = orchestrator.Node(n["host"], int(n["id"].split(":")[1]))
        info = orchestrator.call_agent(SECRET, node, "GET", "/info")
        sock = socket.create_connection((n["host"], info["rpc_port"]), timeout=5)
        sock.settimeout(5)
        challenge = auth.recv_exact(sock, 16)  # el agente nos reta primero
        self.assertEqual(len(challenge), 16)
        sock.sendall(b"PING" + b"\0" * 44)  # respuesta falsa
        with self.assertRaises((ConnectionError, ConnectionResetError, socket.timeout)):
            data = sock.recv(32)
            if not data:
                raise ConnectionError("cerrado")
        sock.close()

    def test_5_recovery_when_node_dies(self):
        before = self.state()["recoveries"]
        victim = self.procs["n3"]
        _proc.killpg(victim.pid, _proc.SIGKILL)  # mata agente y su rpc-server
        self.wait(lambda: self.state()["recoveries"] > before, 30, "detectar caída")
        self.wait(lambda: self.state()["state"] in ("READY", "ERROR"), 60, "recuperación")
        s = self.state()
        self.assertEqual(s["state"], "READY", s.get("error"))
        names = [a["name"] for a in s["plan"]["assignments"]]
        self.assertNotIn("n3", names)
        self.assertEqual(set(names), {"n1", "n2"})
        st, r = self.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual((st, r["mock"]["echo_ok"]), (200, [True, True]))

    def test_6_metrics_and_unload(self):
        st, raw = self.req("GET", "/metrics", raw=True)
        self.assertEqual(st, 200)
        self.assertIn(b'dllm_state{state="READY"} 1', raw)
        self.assertIn(b"dllm_recoveries_total 1", raw)
        self.assertIn(b'dllm_node_up{node="n3"} 0', raw)
        self.assertEqual(self.req("POST", "/api/unload")[0], 200)
        self.assertEqual(self.state()["state"], "IDLE")
        self.assertEqual(self.req("POST", "/v1/chat/completions", {"messages": []})[0], 503)

    def test_7_node_limits_from_panel(self):
        nodes = {n["name"]: n for n in self.state()["nodes"]}
        n1, n2 = nodes["n1"], nodes["n2"]
        self.assertEqual((n1["limits_mode"], n2["limits_mode"]), ("auto", "manual"))
        # nodo en modo auto: el orquestador fija 1,5 GiB y 2 hilos
        st, r = self.req("POST", "/api/node/limits", {"node": n1["id"], "mem_gb": 1.5, "threads": 2})
        self.assertEqual(st, 200, r)
        self.assertEqual((r["cap_bytes"], r["threads"]), (int(1.5 * GiB), 2))
        n1 = next(n for n in self.state()["nodes"] if n["name"] == "n1")
        self.assertEqual((n1["cap_bytes"], n1["threads"], n1["mem_source"]), (int(1.5 * GiB), 2, "fijo"))
        # validación con mensaje claro
        st, r = self.req("POST", "/api/node/limits", {"node": n1["id"], "threads": 99999})
        self.assertEqual(st, 400)
        self.assertIn("hilos", r["error"])
        # nodo en modo manual: el orquestador no puede cambiarlo
        st, r = self.req("POST", "/api/node/limits", {"node": n2["id"], "mem_gb": 1})
        self.assertEqual(st, 400)
        self.assertIn("manual", r["error"])
        # volver al recomendado
        st, r = self.req("POST", "/api/node/limits", {"node": n1["id"], "reset": True})
        self.assertEqual(st, 200)
        n1 = next(n for n in self.state()["nodes"] if n["name"] == "n1")
        self.assertEqual(n1["mem_source"], "recomendado")
        # sin API key no se puede tocar
        if not LAN.startswith("127."):
            self.assertEqual(self.req("POST", "/api/node/limits", {"node": n1["id"], "reset": True}, key=None,
                                      remote=True)[0], 401)

    def test_8_node_settings_load_options_and_telemetry(self):
        nodes = {n["name"]: n for n in self.state()["nodes"]}
        n1, n2 = nodes["n1"], nodes["n2"]
        # ajustes del orquestador por nodo: desactivar, capas fijas, ancho de banda
        self.assertEqual(self.req("POST", "/api/node/settings", {"node": n1["id"], "layers": 0})[0], 400)
        st, r = self.req("POST", "/api/node/settings", {"node": n2["id"], "layers": 20, "bw_gbps": 500})
        self.assertEqual(st, 200, r)
        st, p = self.req("POST", "/api/plan", {"model": "mock.gguf", "ctx": 2048})
        a = {x["name"]: x for x in p["assignments"]}
        self.assertTrue(p["ok"], p.get("reason"))
        self.assertEqual((a["n2"]["layers"], a["n2"]["fixed"], a["n2"]["bw_gbps"]), (20, True, 500.0))
        self.assertEqual(p["assignments"][0]["name"], "n2")  # el ancho de banda fijado lo pone primero
        self.assertEqual(self.req("POST", "/api/node/settings", {"node": n1["id"], "enabled": False})[0], 200)
        st, p = self.req("POST", "/api/plan", {"model": "mock.gguf", "ctx": 2048})
        self.assertIn({"node": "n1", "reason": "desactivado en el panel"}, p["excluded"])
        for n in (n1, n2):
            self.req("POST", "/api/node/settings", {"node": n["id"], "enabled": True, "layers": None, "bw_gbps": None})
        # opciones de carga del modelo → llama-server
        st, r = self.req("POST", "/api/models/settings", {"model": "mock.gguf", "settings": {
            "cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attn": "on", "parallel": 2, "seed": 11}})
        self.assertEqual(st, 200, r)
        self.assertEqual(self.req("POST", "/api/load", {"model": "mock.gguf", "ctx": 2048})[0], 202)
        self.wait(lambda: self.state()["state"] in ("READY", "ERROR"), 60, "carga")
        self.assertEqual(self.state()["state"], "READY", self.state().get("error"))
        st, r = self.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]})
        argv = r["mock"]["argv"]
        for flag, val in (("-ctk", "q8_0"), ("-ctv", "q8_0"), ("-fa", "on"), ("-np", "2")):
            self.assertEqual(argv[argv.index(flag) + 1], val)
        self.assertIn("--metrics", argv)
        self.assertEqual(r["mock"]["received"]["seed"], 11)
        # telemetría: historial por nodo y tokens/s calculados desde /metrics de llama-server
        for _ in range(3):
            self.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]})
            time.sleep(1.2)
        self.wait(lambda: (self.req("GET", "/api/telemetry")[1]["llama"].get("tok_s") or 0) > 0, 15, "tokens/s")
        t = self.req("GET", "/api/telemetry")[1]
        n = next(x for x in t["nodes"] if x["name"] == "n1")
        self.assertTrue(n["hist"])
        self.assertIn("ram_total", n["now"])
        self.assertIsNone(n["now"].get("gpu"))  # nodo CPU
        self.req("POST", "/api/models/settings", {"model": "mock.gguf", "settings": {}})
        self.req("POST", "/api/unload")


if __name__ == "__main__":
    unittest.main(verbosity=2)
