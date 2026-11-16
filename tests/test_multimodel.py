# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Varios modelos a la vez (slots), libro de memoria, enrutado por «model» y carga bajo demanda con LRU.
Binarios simulados (tests/mockbin); procesos reales de agente y orquestador."""
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402

GiB = 1 << 30
SECRET = "secreto-multimodelo-1234567890"


@unittest.skipIf(os.name == "nt", "usa grupos de procesos POSIX y mocks con shebang")
class MultiModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        models = os.path.join(cls.tmp, "models")
        os.makedirs(models)
        for n in ("a", "b", "c"):
            write_gguf(os.path.join(models, f"{n}.gguf"), 3 * GiB)
        write_gguf(os.path.join(models, "emb.gguf"), 64 << 20)
        write_gguf(os.path.join(models, "aux.gguf"), 64 << 20)
        write_gguf(os.path.join(models, "huge.gguf"), 14 * GiB)
        cls.env = dict(os.environ, PYTHONPATH=ROOT, DLLM_HOME=os.path.join(cls.tmp, "home"))
        cls.procs = []
        cls.op, bp = free_port(), free_port()
        mock = os.path.join(HERE, "mockbin")
        for name in ("n1", "n2"):
            cls._spawn(["agent", "--secret", SECRET, "--name", name, "--port", str(free_port()),
                        "--rpc-port", str(free_port()), "--bin-dir", mock, "--backend", "cpu", "--mem-gb", "5",
                        "--bw-gbps", "50", "--announce-to", f"127.0.0.1:{bp}", "--beacon-interval", "0.5",
                        "--limits-mode", "manual"])
        cls._spawn(["up", "--secret", SECRET, "--models-dir", models, "--port", str(cls.op), "--bin-dir", mock,
                    "--beacon-port", str(bp), "--node-timeout", "3", "--probe-interval", "0.5", "--on-demand", "--queue-timeout", "1.5"])
        try:
            cls.wait(lambda: sum(n["up"] for n in cls.api("GET", "/api/state")[1]["nodes"]) == 2, 30, "2 nodos")
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def _spawn(cls, args):
        log = open(os.path.join(cls.tmp, f"{args[0]}-{len(cls.procs)}.log"), "w")
        cls.procs.append(subprocess.Popen([sys.executable, "-m", "dllm"] + args, cwd=ROOT, env=cls.env,
                                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True))

    @classmethod
    def tearDownClass(cls):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for p in cls.procs:
                try:
                    os.killpg(p.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(1)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def api(cls, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.op, timeout=120)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data or b"{}")
        except ValueError:
            return r.status, data

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
        logs = "\n".join(open(os.path.join(cls.tmp, f)).read()[-1500:] for f in sorted(os.listdir(cls.tmp))
                         if f.endswith(".log"))
        raise AssertionError(f"tiempo agotado: {what}\n{logs}")

    def slots(self):
        return {s["model"]: s for s in self.api("GET", "/api/state")[1]["slots"] if s["model"]}

    def chat(self, model, **kw):
        return self.api("POST", "/v1/chat/completions", dict({"model": model, "messages": []}, **kw))

    def test_1_slots_routing_and_lru(self):
        # modelo principal fijado (slot 0)
        st, r = self.api("POST", "/api/load", {"model": "a.gguf", "ctx": 2048})
        self.assertEqual((st, r["slot"]), (202, 0))
        self.wait(lambda: self.slots().get("a.gguf", {}).get("state") == "READY", 60, "a listo")
        # b: bajo demanda → slot nuevo en el otro nodo (el ledger impide meterlo donde ya está a)
        st, r = self.chat("b.gguf")
        self.assertEqual(st, 200, r)
        self.assertTrue(r["mock"]["argv"][1].endswith("b.gguf"))
        self.assertEqual(r["mock"]["echo_ok"], [True])  # canal autenticado propio del slot hasta su rpc-server
        s = self.slots()
        self.assertEqual({k: v["pinned"] for k, v in s.items()}, {"a.gguf": True, "b.gguf": False})
        self.assertNotEqual(s["a.gguf"]["llama_port"], s["b.gguf"]["llama_port"])
        self.assertNotEqual(s["a.gguf"]["nodes"], s["b.gguf"]["nodes"])
        led = self.api("GET", "/api/state")[1]["ledger"]
        self.assertTrue(all(len(v["ram_reserved"]) == 1 for v in led.values()), led)  # nodos solo-CPU: reservan RAM
        # el enrutado por «model» devuelve a cada uno lo suyo y /v1/models lista los dos
        self.assertTrue(self.chat("a.gguf")[1]["mock"]["argv"][1].endswith("a.gguf"))
        ids = {m["id"] for m in self.api("GET", "/v1/models")[1]["data"]}
        self.assertTrue({"a.gguf", "b.gguf", "c.gguf"} <= ids)
        # streaming SSE por el slot de b
        c = http.client.HTTPConnection("127.0.0.1", self.op, timeout=60)
        c.request("POST", "/v1/chat/completions", json.dumps({"model": "b.gguf", "messages": [], "stream": True}),
                  {"Content-Type": "application/json"})
        raw = c.getresponse().read()
        self.assertIn(b"tok0", raw)
        self.assertIn(b"[DONE]", raw)
        # c no cabe: expulsa b (on-demand, el menos usado); a está fijado y se queda
        self.chat("a.gguf")  # a usado más recientemente que b
        st, r = self.chat("c.gguf")
        self.assertEqual(st, 200, r)
        s = self.slots()
        self.assertEqual(set(s), {"a.gguf", "c.gguf"})
        self.assertTrue(s["a.gguf"]["pinned"])
        # nombre desconocido: como siempre, atiende el modelo principal
        self.assertTrue(self.chat("gpt-4")[1]["mock"]["argv"][1].endswith("a.gguf"))

    def test_2_embeddings_driver_and_capability(self):
        st, r = self.api("POST", "/api/models/settings", {"model": "emb.gguf", "settings": {"embeddings": True}})
        self.assertEqual(st, 200, r)
        st, r = self.api("POST", "/v1/embeddings", {"model": "emb.gguf", "input": "hola"})
        self.assertEqual(st, 200, r)
        self.assertIn("--embeddings", r["mock"]["argv"])
        s = self.slots()["emb.gguf"]
        self.assertEqual(len(s["nodes"]), 1)  # placement auto → un único nodo (Task Affinity)
        # un modelo de embeddings no atiende chat, y uno de chat no atiende embeddings
        st, r = self.chat("emb.gguf")
        self.assertEqual(st, 400)
        self.assertIn("no admite chat", r["error"]["message"])
        st, r = self.api("POST", "/v1/embeddings", {"model": "a.gguf", "input": "x"})
        self.assertEqual(st, 400)

    def test_3_drop_slot_releases_memory(self):
        for slot in [v["slot"] for v in self.slots().values() if v["slot"]]:
            self.assertEqual(self.api("POST", "/api/unload", {"slot": slot})[0], 200)
        led = self.api("GET", "/api/state")[1]["ledger"]
        self.assertEqual(sum(len(v["ram_reserved"]) for v in led.values()), 1)  # solo queda el principal (a)

    def set_model(self, model, settings):
        st, r = self.api("POST", "/api/models/settings", {"model": model, "settings": settings})
        self.assertEqual(st, 200, r)

    def test_4_busy_slot_is_never_killed_queue_429_and_503(self):
        import threading
        import urllib.error  # noqa: F401
        # b bajo demanda y ocupado con una petición lenta; c necesita su sitio → cola → 429 con Retry-After
        self.assertEqual(self.chat("b.gguf")[0], 200)
        slow = {}
        t = threading.Thread(target=lambda: slow.update(r=self.chat("b.gguf", _sleep=4)))
        t.start()
        self.wait(lambda: self.slots().get("b.gguf", {}).get("activity") == "BUSY", 5, "b ocupado")
        c = http.client.HTTPConnection("127.0.0.1", self.op, timeout=60)
        t0 = time.time()
        c.request("POST", "/v1/chat/completions", json.dumps({"model": "c.gguf", "messages": []}),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        body = json.loads(r.read())
        self.assertEqual(r.status, 429, body)
        self.assertTrue(r.getheader("Retry-After"))
        self.assertEqual(body["error"]["code"], "rate_limit_exceeded")  # formato de error OpenAI
        self.assertGreaterEqual(time.time() - t0, 1.0)  # esperó en la cola
        t.join(20)
        self.assertEqual(slow["r"][0], 200)  # la generación en curso terminó sin que la mataran
        self.assertIn("b.gguf", self.slots())
        # ahora b está IDLE: c entra expulsándolo
        self.assertEqual(self.chat("c.gguf")[0], 200)
        # un modelo que no cabe ni sin los on-demand se rechaza antes de lanzar nada, sin esperar
        n_slots = len(self.api("GET", "/api/state")[1]["slots"])
        t0 = time.time()
        st, body = self.chat("huge.gguf")
        self.assertEqual(st, 503, body)
        self.assertLess(time.time() - t0, 1.2)
        self.assertEqual(body["error"]["type"], "server_error")
        self.assertEqual(len(self.api("GET", "/api/state")[1]["slots"]), n_slots)

    def test_5_cpu_only_policy_runs_on_host_ram(self):
        self.set_model("aux.gguf", {"embeddings": True, "device_policy": "cpu_only"})
        st, r = self.api("POST", "/v1/embeddings", {"model": "aux.gguf", "input": "x"})
        self.assertEqual(st, 200, r)
        argv = r["mock"]["argv"]
        self.assertEqual(argv[argv.index("-ngl") + 1], "0")
        self.assertNotIn("--rpc", argv)  # sin nodos remotos
        s = self.slots()["aux.gguf"]
        self.assertEqual(s["nodes"], [])
        self.assertEqual(s["policy"], "cpu_only")
        led = self.api("GET", "/api/state")[1]["ledger"]
        self.assertIn("@host", led)
        self.assertGreater(list(led["@host"]["ram_reserved"].values())[0], 0)
        self.assertGreaterEqual(s["llama_port"], 18081)  # rango interno de slots

    def test_6_policies_in_plan(self):
        # nodos solo-CPU: gpu_strict los descarta y gpu_with_cpu_fallback cae a CPU del orquestador (si hay RAM)
        self.set_model("a.gguf", {"device_policy": "gpu_strict"})
        _, p = self.api("POST", "/api/plan", {"model": "a.gguf", "ctx": 2048})
        self.assertFalse(p["ok"])
        self.assertTrue(any("no tiene GPU" in e["reason"] for e in p["excluded"]))
        self.set_model("a.gguf", {"device_policy": "gpu_with_cpu_fallback"})
        _, p = self.api("POST", "/api/plan", {"model": "a.gguf", "ctx": 2048})
        self.assertEqual(p["policy"], "gpu_with_cpu_fallback")
        self.assertIn("-ngl 0", " ".join(p["warnings"]) if p["ok"] else "-ngl 0")
        self.set_model("a.gguf", {})  # restablecer

    def test_7_crash_reason_is_reported_and_slot_recovers(self):
        self.wait(lambda: self.slots().get("a.gguf", {}).get("state") == "READY", 30, "a listo")
        st, _ = self.chat("a.gguf", _crash=True)  # llama-server aborta a mitad de la petición
        self.assertGreaterEqual(st, 500)
        self.wait(lambda: self.api("GET", "/api/state")[1].get("last_crash"), 15, "caída registrada")
        state = self.api("GET", "/api/state")[1]
        self.assertEqual(state["last_crash"]["code"], 134)
        self.assertTrue(any("GGML_ASSERT" in l for l in state["last_crash"]["lines"]))
        self.assertTrue(any("(código 134)" in e["msg"] for e in state["events"]))
        self.wait(lambda: self.slots().get("a.gguf", {}).get("state") == "READY", 60, "recuperado")


if __name__ == "__main__":
    unittest.main()
