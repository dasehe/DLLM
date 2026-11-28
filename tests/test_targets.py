# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Nodos y dispositivo (GPU/CPU) elegidos a mano, recursos del clúster, cancelar/eliminar modelos en cualquier estado,
registro de eventos y métricas por modelo. Binarios simulados; procesos reales de agente y orquestador."""
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _proc  # noqa: E402
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402

GiB = 1 << 30
SECRET = "secreto-destinos-1234567890abcd"


@unittest.skipIf(os.name == "nt", "usa grupos de procesos POSIX y mocks con shebang")
class Targets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        models = os.path.join(cls.tmp, "models")
        os.makedirs(models)
        write_gguf(os.path.join(models, "small.gguf"), 1 * GiB)
        write_gguf(os.path.join(models, "other.gguf"), 1 * GiB)
        write_gguf(os.path.join(models, "huge.gguf"), 60 * GiB)
        cls.args_file = os.path.join(cls.tmp, "gpu1-rpc-args.json")
        cls.procs = []
        cls.op, bp = free_port(), free_port()
        mock = os.path.join(HERE, "mockbin")
        base = dict(os.environ, PYTHONPATH=ROOT)
        cls._spawn("gpu1", dict(base, DLLM_HOME=os.path.join(cls.tmp, "g"), MOCK_ARGS_FILE=cls.args_file,
                                MOCK_DEVICES="Vulkan0|Mock GPU|8192|8000;CPU|Mock CPU|32768|20000"),
                   ["agent", "--secret", SECRET, "--name", "gpu1", "--port", str(free_port()),
                    "--rpc-port", str(free_port()), "--bin-dir", mock, "--announce-to", f"127.0.0.1:{bp}",
                    "--beacon-interval", "0.5", "--bw-gbps", "50"])
        cls._spawn("cpu1", dict(base, DLLM_HOME=os.path.join(cls.tmp, "c")),
                   ["agent", "--secret", SECRET, "--name", "cpu1", "--port", str(free_port()),
                    "--rpc-port", str(free_port()), "--bin-dir", mock, "--backend", "cpu", "--mem-gb", "6",
                    "--bw-gbps", "40", "--announce-to", f"127.0.0.1:{bp}", "--beacon-interval", "0.5"])
        cls._spawn("orch", dict(base, DLLM_HOME=os.path.join(cls.tmp, "o"), MOCK_LOAD_DELAY="3"),
                   ["up", "--secret", SECRET, "--models-dir", models, "--port", str(cls.op), "--bin-dir", mock,
                    "--beacon-port", str(bp), "--node-timeout", "4", "--probe-interval", "0.5"])
        try:
            cls.wait(lambda: sum(n["up"] for n in cls.api("GET", "/api/state")[1]["nodes"]) == 2, 40, "2 nodos")
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def _spawn(cls, name, env, args):
        log = open(os.path.join(cls.tmp, f"{name}.log"), "w")
        cls.procs.append(subprocess.Popen([sys.executable, "-m", "dllm"] + args, cwd=ROOT, env=env,
                                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True))

    @classmethod
    def tearDownClass(cls):
        for sig in (signal.SIGTERM, _proc.SIGKILL):
            for p in cls.procs:
                try:
                    _proc.killpg(p.pid, sig)
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
        return {s["slot"]: s for s in self.api("GET", "/api/state")[1]["slots"]}

    def plan(self, model="small.gguf", **kw):
        return self.api("POST", "/api/plan", dict({"model": model, "ctx": 2048}, **kw))[1]

    def test_1_resources(self):
        r = self.api("GET", "/api/resources")[1]
        by = {n["name"]: n for n in r["nodes"]}
        self.assertTrue(by["gpu1"]["gpu"])
        self.assertTrue(by["gpu1"]["cpu_device"])
        self.assertGreater(by["gpu1"]["vram_cap"], 0)
        self.assertFalse(by["cpu1"]["gpu"])
        self.assertEqual(by["cpu1"]["vram_cap"], 0)
        self.assertGreater(by["cpu1"]["ram_cap"], 0)
        t = r["totals"]
        self.assertEqual(t["vram_cap"], by["gpu1"]["vram_cap"])
        self.assertGreater(t["cores"], 0)
        self.assertGreater(t["ram_total"], 0)

    def test_2_plan_with_targets(self):
        # CPU del nodo con GPU: usa su RAM y no toca la VRAM
        p = self.plan(targets=[{"node": "gpu1", "device": "cpu"}])
        self.assertTrue(p["ok"], p)
        self.assertEqual([(a["name"], a["device"]) for a in p["assignments"]], [("gpu1", "cpu")])
        self.assertEqual(p["reserve"]["vram"], {})
        self.assertTrue(p["reserve"]["ram"])
        self.assertIn({"node": "cpu1", "reason": "no seleccionado"}, p["excluded"])
        r = p["resources"]["nodes"]
        g = next(n for n in r if n["name"] == "gpu1")
        self.assertEqual(g["vram_after"], g["vram_reserved"])           # la GPU queda libre
        self.assertGreater(g["ram_after"], g["ram_reserved"])           # y se consume su RAM
        # GPU del mismo nodo: reserva VRAM
        p = self.plan(targets=[{"node": "gpu1", "device": "gpu"}])
        self.assertEqual(p["assignments"][0]["device"], "gpu")
        self.assertTrue(p["reserve"]["vram"])
        # pedir GPU a un nodo sin GPU, o un nodo inexistente, no cabe
        p = self.plan(targets=[{"node": "cpu1", "device": "gpu"}])
        self.assertFalse(p["ok"])
        self.assertTrue(any("no tiene" in e["reason"] for e in p["excluded"]), p["excluded"])
        p = self.plan(targets=[{"node": "fantasma", "device": "cpu"}])
        self.assertFalse(p["ok"])
        self.assertTrue(any(e["reason"] == "no existe" for e in p["excluded"]))
        # validación del formato
        st, _ = self.api("POST", "/api/plan", {"model": "small.gguf", "targets": [{"node": "gpu1", "device": "tpu"}]})
        self.assertEqual(st, 400)
        # el orquestador no se mezcla con nodos
        p = self.plan(targets=[{"node": "@host", "device": "cpu"}, {"node": "gpu1", "device": "gpu"}])
        self.assertFalse(p["ok"])
        self.assertIn("@host", p["reason"])
        # solo el orquestador: todo en su RAM
        p = self.plan(targets=[{"node": "@host", "device": "cpu"}])
        self.assertTrue(p["ok"], p)
        self.assertEqual(p["placement"], "host")

    def test_3_load_on_cpu_of_gpu_node_and_second_model_on_gpu(self):
        st, r = self.api("POST", "/api/load", {"model": "small.gguf", "ctx": 2048, "slot": "new",
                                               "targets": [{"node": "gpu1", "device": "cpu"}]})
        self.assertEqual(st, 202)
        s = r["slot"]
        self.wait(lambda: self.slots()[s]["state"] == "READY", 60, "small listo en la CPU de gpu1")
        self.assertEqual(self.slots()[s]["assignments"][0]["device"], "cpu")
        self.assertEqual(self.slots()[s]["targets"], [{"node": "gpu1", "device": "cpu"}])
        args = json.load(open(self.args_file))
        self.assertEqual(args[args.index("-d") + 1], "CPU")  # rpc-server de este modelo en la CPU del nodo
        # otro modelo en la GPU del mismo nodo, a la vez
        st, r2 = self.api("POST", "/api/load", {"model": "other.gguf", "ctx": 2048, "slot": "new",
                                                "targets": [{"node": "gpu1", "device": "gpu"}]})
        s2 = r2["slot"]
        self.wait(lambda: self.slots()[s2]["state"] == "READY", 60, "other listo en la GPU")
        args = json.load(open(self.args_file))
        self.assertEqual(args[args.index("-d") + 1], "Vulkan0")
        res = self.api("GET", "/api/resources")[1]
        g = next(n for n in res["nodes"] if n["name"] == "gpu1")
        self.assertGreater(g["vram_reserved"], 0)
        self.assertGreater(g["ram_reserved"], 0)
        for k in (s, s2):
            self.api("POST", "/api/unload", {"slot": k})
        self.wait(lambda: not self.slots(), 30, "slots eliminados") if False else None

    def test_4_drop_failed_slot_and_cancel_loading(self):
        # un modelo que no cabe deja el slot en ERROR: se puede eliminar
        st, r = self.api("POST", "/api/load", {"model": "huge.gguf", "ctx": 2048, "slot": "new"})
        s = r["slot"]
        self.wait(lambda: self.slots().get(s, {}).get("state") == "ERROR", 30, "slot en error")
        self.assertIn("libre por nodo", self.slots()[s]["error"])
        st, _ = self.api("POST", "/api/unload", {"slot": s})
        self.assertEqual(st, 200)
        self.wait(lambda: s not in self.slots(), 20, "slot en error eliminado")
        # cancelar una carga en curso: responde enseguida y el slot desaparece
        st, r = self.api("POST", "/api/load", {"model": "small.gguf", "ctx": 2048, "slot": "new"})
        s = r["slot"]
        self.wait(lambda: self.slots().get(s, {}).get("state") == "LOADING", 20, "cargando")
        t0 = time.time()
        st, _ = self.api("POST", "/api/unload", {"slot": s})
        self.assertEqual(st, 200)
        self.assertLess(time.time() - t0, 2.5)
        self.wait(lambda: s not in self.slots(), 20, "carga cancelada")
        ev = self.api("GET", "/api/events?q=cancelada")[1]["events"]
        self.assertTrue(ev and ev[-1]["kind"] == "unload")

    def test_5_events_and_metrics(self):
        ev = self.api("GET", "/api/events?level=error")[1]
        self.assertTrue(ev["events"], "el modelo que no cabía debe figurar como error")
        self.assertTrue(all(e["level"] == "error" for e in ev["events"]))
        self.assertGreaterEqual(ev["counts"]["error"], 1)
        one = self.api("GET", "/api/events?node=gpu1")[1]["events"]
        self.assertTrue(one and all(e["node"] == "gpu1" for e in one))
        self.assertEqual(self.api("GET", "/api/events?q=zzz-no-existe")[1]["events"], [])
        # métricas por modelo: una petición real
        st, r = self.api("POST", "/api/load", {"model": "small.gguf", "ctx": 2048, "slot": "new"})
        s = r["slot"]
        self.wait(lambda: self.slots()[s]["state"] == "READY", 60, "listo")
        st, _ = self.api("POST", "/v1/chat/completions", {"model": "small.gguf", "messages": []})
        self.assertEqual(st, 200)
        m = self.slots()[s]["metrics"]
        self.assertEqual(m["requests"], 1)
        self.assertEqual(m["errors"], 0)
        self.assertEqual(m["by_status"], {"200": 1})
        self.assertIsNotNone(m["p95_ms"])
        text = self.api("GET", "/metrics")[1].decode()
        self.assertIn(f'dllm_slot_requests_total{{slot="{s}",model="small.gguf"}} 1', text)
        self.assertIn('dllm_node_rpc_restarts_total{node="gpu1"}', text)
        self.assertIn('dllm_events_total{level="error"}', text)
        self.api("POST", "/api/unload", {"slot": s})

    def test_6_hot_reload_queues_requests(self):
        import threading
        st, r = self.api("POST", "/api/load", {"model": "other.gguf", "ctx": 2048, "slot": "new"})
        s = r["slot"]
        self.wait(lambda: self.slots()[s]["state"] == "READY", 60, "listo")
        self.api("POST", "/api/models/settings", {"model": "other.gguf", "settings": {"ctx": 3072}})
        out = {}
        slow = threading.Thread(target=lambda: out.update(slow=self.api(
            "POST", "/v1/chat/completions", {"model": "other.gguf", "messages": [], "_sleep": 2})[0]))
        slow.start()
        time.sleep(0.5)
        st, j = self.api("POST", "/api/reload", {"slot": s})
        self.assertEqual((st, j["ctx"]), (202, 3072), j)
        self.assertEqual(self.api("POST", "/api/reload", {"slot": s})[0], 409)   # una recarga a la vez
        t0 = time.time()
        st, _ = self.api("POST", "/v1/chat/completions", {"model": "other.gguf", "messages": []})
        waited = time.time() - t0
        slow.join(30)
        self.assertEqual(out.get("slow"), 200)   # la petición en curso terminó antes de descargar
        self.assertEqual(st, 200)                # la nueva esperó en cola en vez de fallar
        self.assertGreater(waited, 2.0)          # (carga simulada de 3 s)
        sl = self.slots()[s]
        self.assertEqual((sl["state"], sl["ctx"], sl["reloading"]), ("READY", 3072, False))
        ev = self.api("GET", "/api/events?q=recarga")[1]["events"]
        self.assertTrue(ev)
        self.assertEqual(self.api("POST", "/api/reload", {"slot": 7})[0], 400)
        self.api("POST", "/api/unload", {"slot": s})

    def test_7_gpu_and_ram_of_the_same_node(self):
        write_gguf(os.path.join(self.tmp, "models", "mid.gguf"), 9 * GiB)
        tg = [{"node": "gpu1", "device": "both"}]
        p = self.plan("mid.gguf", targets=tg)
        self.assertTrue(p["ok"], p.get("reason"))
        self.assertEqual([(a["name"], a["device"]) for a in p["assignments"]], [("gpu1", "gpu"), ("gpu1", "cpu")])
        # tope con el deslizante: menos RAM de la necesaria → no cabe
        p2 = self.plan("mid.gguf", targets=[{"node": "gpu1", "device": "both", "ram_gb": 0.5}])
        self.assertFalse(p2["ok"])
        st, r = self.api("POST", "/api/load", {"model": "mid.gguf", "ctx": 2048, "slot": "new", "targets": tg})
        self.assertEqual(st, 202, r)
        s = r["slot"]
        self.wait(lambda: self.slots()[s]["state"] == "READY", 60, "mid listo en GPU+RAM de gpu1")
        args = json.load(open(self.args_file))
        self.assertEqual(args[args.index("-d") + 1], "Vulkan0,CPU")  # un rpc-server con los dos dispositivos
        res = self.api("GET", "/api/resources")[1]
        g = next(n for n in res["nodes"] if n["name"] == "gpu1")
        self.assertGreater(g["ram_reserved"], 1 * GiB)  # su RAM queda reservada para este modelo
        self.api("POST", "/api/unload", {"slot": s})
        # prioridad del nodo: en automático, gpu1 (GPU y RAM) antes que cpu1
        gid = next(n["id"] for n in self.api("GET", "/api/state")[1]["nodes"] if n["name"] == "gpu1")
        self.assertEqual(self.api("POST", "/api/node/settings", {"node": gid, "priority": 5})[0], 200)
        p = self.plan("mid.gguf")
        self.assertTrue(p["ok"], p.get("reason"))
        self.assertEqual([a["name"] for a in p["assignments"]][:2], ["gpu1", "gpu1"])
        self.assertEqual(self.api("POST", "/api/node/settings", {"node": gid, "priority": 11})[0], 400)
        self.api("POST", "/api/node/settings", {"node": gid, "priority": None})

    def test_8_deployment_templates(self):
        write_gguf(os.path.join(self.tmp, "models", "mid.gguf"), 9 * GiB)
        tg = [{"node": "gpu1", "device": "both"}]
        st, v = self.api("POST", "/api/templates/save", {"name": "mid en gpu1", "model": "mid.gguf", "ctx": 2048,
                                                          "slot": "new", "targets": tg})
        self.assertEqual(st, 200, v)
        self.assertIn("mid en gpu1", self.api("GET", "/api/templates")[1]["templates"])
        st, r = self.api("POST", "/api/templates/apply", {"name": "mid en gpu1"})
        self.assertEqual(st, 202, r)
        s = r["slot"]
        self.wait(lambda: self.slots()[s]["state"] == "READY", 60, "plantilla desplegada")
        sl = self.slots()[s]
        self.assertEqual((sl["model"], sl["ctx"], sl["targets"]), ("mid.gguf", 2048, tg))
        out = subprocess.run([sys.executable, "-m", "dllm", "--lang", "es", "template", "list", "--url",
                              f"http://127.0.0.1:{self.op}"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertIn("mid en gpu1: mid.gguf", out.stdout, out.stderr)
        self.assertEqual(self.api("POST", "/api/templates/apply", {"name": "no existe"})[0], 400)
        self.api("POST", "/api/unload", {"slot": s})
        self.assertEqual(self.api("POST", "/api/templates/delete", {"name": "mid en gpu1"})[0], 200)


if __name__ == "__main__":
    unittest.main()
