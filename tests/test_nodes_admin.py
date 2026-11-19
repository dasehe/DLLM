# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Administración de nodos: alta por IP, búsqueda en la red, medición individual y conjunta, modo forzado para redes
lentas (con confirmación), eliminar nodos y modelos por nodo. Procesos reales de agente y orquestador con binarios
simulados, más pruebas en proceso de la medición conjunta (el enlace real a 127.0.0.1 siempre es «local»)."""
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from dllm import discovery, orchestrator, scheduler  # noqa: E402
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402

GiB = 1 << 30
SECRET = "secreto-nodos-admin-1234567890ab"


class TestDiscovery(unittest.TestCase):
    def test_valid_host_and_port(self):
        self.assertEqual(discovery.valid_host(" 192.168.1.5 "), "192.168.1.5")
        self.assertEqual(discovery.valid_host("nodo-1.lan"), "nodo-1.lan")
        for bad in ("", "http://x", "a/b", "a b", "x;y", "-x", "a" * 300):
            with self.assertRaises(ValueError, msg=bad):
                discovery.valid_host(bad)
        self.assertEqual(discovery.valid_port(None), 8765)
        for bad in ("0", "70000", "abc"):
            with self.assertRaises(ValueError):
                discovery.valid_port(bad)

    def test_ranges(self):
        self.assertEqual(len(discovery.hosts_of("192.168.1.0/24")), 254)
        self.assertEqual(discovery.hosts_of("10.0.0.5-7"), ["10.0.0.5", "10.0.0.6", "10.0.0.7"])
        self.assertEqual(discovery.default_range("192.168.7.33"), "192.168.7.0/24")
        for bad in ("8.8.8.0/24", "10.0.0.0/8", "10.0.0.9-3", "nada", "10.0.0.0/16"):
            with self.assertRaises(ValueError, msg=bad):
                discovery.hosts_of(bad)

    def test_probe_classifies_agents(self):
        def serve(status, body):
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *a):
                    pass
            s = HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
            return s.server_address[1]
        self.assertEqual(discovery.probe("127.0.0.1", serve(200, b'{"state":"desconocida"}'))["state"], "libre")
        self.assertEqual(discovery.probe("127.0.0.1", serve(404, b'{"error":"este nodo ya est\xc3\xa1 emparejado"}'))
                         ["state"], "emparejado")
        self.assertIsNone(discovery.probe("127.0.0.1", serve(200, b"<html>")))   # otro servicio web
        self.assertIsNone(discovery.probe("127.0.0.1", free_port()))              # puerto cerrado


class TestWizardOrchIp(unittest.TestCase):
    def test_orch_ip_only_for_nodes_and_validated(self):
        from dllm import wizard
        sp = wizard.Setup(dry_run=True)
        n = sp.normalize({"role": "node", "orch_ip": "192.168.1.10"})
        self.assertEqual(n["orch_ip"], "192.168.1.10:50999")
        n = sp.normalize({"role": "node", "orch_ip": "orq.lan:50111", "beacon_port": 50111})
        self.assertEqual(n["orch_ip"], "orq.lan:50111")
        self.assertIsNone(sp.normalize({"role": "both", "orch_ip": "192.168.1.10"})["orch_ip"])
        with self.assertRaises(ValueError):
            sp.normalize({"role": "node", "orch_ip": "http://x/y"})


class TestSlowMode(unittest.TestCase):
    NODES = [{"id": "a", "name": "rapido", "backend": "cpu", "cap_bytes": 8 * GiB, "bw_gbps": 20, "mbps": 2000,
              "rtt_ms": 0.5},
             {"id": "b", "name": "lento", "backend": "cpu", "cap_bytes": 8 * GiB, "bw_gbps": 20, "mbps": 300,
              "rtt_ms": 1.0}]
    MODEL = {"name": "m", "size": 9 * GiB, "n_layers": 32, "n_kv_heads": 8, "head_dim": 128}

    def test_slow_excluded_unless_forced(self):
        p = scheduler.plan(self.MODEL, self.NODES, 2048, 800)
        self.assertFalse(p["ok"])
        self.assertIn("lento", [e["node"] for e in p["excluded"]])
        self.assertIn("usarlo igualmente", " ".join(e["reason"] for e in p["excluded"]))
        forced = [dict(n, allow_slow=n["id"] == "b") for n in self.NODES]
        p = scheduler.plan(self.MODEL, forced, 2048, 800)
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual({a["name"] for a in p["assignments"]}, {"rapido", "lento"})
        self.assertTrue(any("modo forzado" in w and "lento" in w for w in p["warnings"]))


class TestBenchJoint(unittest.TestCase):
    """Medición uno a uno y en conjunto, en proceso y con la red simulada."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["DLLM_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "DLLM_HOME", None)
        cfg = SimpleNamespace(min_mbps=800.0, events_file=None, beacon_port=1, models_dir=self.tmp, llama_bin=None)
        self.reg = orchestrator.Registry("s" * 24)
        self.cl = orchestrator.Cluster(cfg, self.reg)
        self.nodes = []
        for i, (name, mbps) in enumerate((("a", 1000), ("b", 1000), ("c", 200))):
            n = self.reg.add(f"10.9.0.{i + 1}", 8765)
            n.info, n.last_seen = {"name": name, "rpc_port": 50051}, time.time()
            n.mbps = mbps
            self.nodes.append(n)

    def run_bench(self, joint_ratio):
        def fake_bench(secret, node):
            return {"rtt_ms": 0.4, "mbps": node.mbps, "up_mbps": node.mbps, "down_mbps": node.mbps, "at": time.time(),
                    "local": False, "tier": 2}

        def fake_probe(host, port, secret):
            n = next(x for x in self.nodes if x.host == host)
            return {"rtt_ms": 0.9, "mbps": int(n.mbps * joint_ratio), "at": time.time()}
        with mock.patch.object(orchestrator, "bench_node", fake_bench), \
                mock.patch.object(orchestrator, "probe_link", fake_probe):
            self.cl.start_bench()
            end = time.time() + 20
            while self.cl.jobs["bench"]["state"] == "running" and time.time() < end:
                time.sleep(0.05)
        return self.cl.jobs["bench"]

    def test_individual_then_joint(self):
        job = self.run_bench(0.4)
        self.assertEqual(job["state"], "done", job)
        self.assertEqual([i["node"] for i in job["items"]], ["a", "b", "c"])
        self.assertEqual({i["node"]: i["slow"] for i in job["items"]}, {"a": False, "b": False, "c": True})
        self.assertEqual(len(job["joint"]["nodes"]), 3)
        self.assertEqual(job["joint"]["nodes"][0]["ratio"], 0.4)
        self.assertEqual(self.nodes[0].net["joint_mbps"], 400)
        lv = [e for e in self.cl.elog.query(level="warn")]
        txt = " ".join(e["msg"] for e in lv)
        self.assertIn("c: red de 200 Mbps", txt)
        self.assertIn("se reparte entre ellos", txt)

    def test_no_warning_when_link_scales(self):
        job = self.run_bench(0.95)
        self.assertNotIn("se reparte entre ellos", " ".join(e["msg"] for e in self.cl.elog.query(level="warn")))
        self.assertEqual(job["state"], "done")

    def test_only_one_job_at_a_time(self):
        self.cl.jobs["bench"]["state"] = "running"
        with self.assertRaises(orchestrator.Busy):
            self.cl.start_bench()

    def test_forget_blocks_beacons_and_refuses_in_use(self):
        n = self.nodes[0]
        c = SimpleNamespace(used=[n.id])
        self.cl.slots[1] = c
        with self.assertRaises(orchestrator.Busy):
            self.cl.forget_node(n.id)
        del self.cl.slots[1]
        r = self.cl.forget_node(n.id)
        self.assertTrue(r["was_up"])
        self.assertIsNone(self.reg.get(n.id))
        self.assertGreater(self.reg.banned[n.host], time.time())
        with self.assertRaises(ValueError):
            self.cl.forget_node(n.id)

    def test_slow_mode_needs_confirmation(self):
        n = self.nodes[2]
        with self.assertRaises(ValueError):
            self.cl.set_node_settings({"node": n.id, "allow_slow": True})
        with mock.patch.object(self.cl.reg, "probe"):
            r = self.cl.set_node_settings({"node": n.id, "allow_slow": True, "confirm": True})
        self.assertTrue(r["settings"]["allow_slow"])
        r = self.cl.set_node_settings({"node": n.id, "allow_slow": False})
        self.assertNotIn("allow_slow", r["settings"])


@unittest.skipIf(os.name == "nt", "usa grupos de procesos POSIX y mocks con shebang")
class NodesAdminE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        models = os.path.join(cls.tmp, "models")
        os.makedirs(models)
        write_gguf(os.path.join(models, "small.gguf"), 1 * GiB)
        cls.procs = []
        cls.op, cls.bp = free_port(), free_port()
        cls.hidden_port, cls.free_agent_port = free_port(), free_port()
        mock_bin = os.path.join(HERE, "mockbin")
        base = dict(os.environ, PYTHONPATH=ROOT)
        cls._spawn("vis", dict(base, DLLM_HOME=os.path.join(cls.tmp, "v")),
                   ["agent", "--secret", SECRET, "--name", "vis", "--port", str(free_port()),
                    "--rpc-port", str(free_port()), "--bin-dir", mock_bin, "--backend", "cpu", "--mem-gb", "6",
                    "--announce-to", f"127.0.0.1:{cls.bp}", "--beacon-interval", "0.5"])
        # el segundo agente no emite beacons (no se anuncia a nadie): solo se le llega por IP
        cls._spawn("hidden", dict(base, DLLM_HOME=os.path.join(cls.tmp, "h")),
                   ["agent", "--secret", SECRET, "--name", "hidden", "--port", str(cls.hidden_port),
                    "--rpc-port", str(free_port()), "--bin-dir", mock_bin, "--backend", "cpu", "--mem-gb", "6",
                    "--announce-to", "127.0.0.1:9", "--beacon-interval", "5"])
        # agente sin secreto: espera a que lo reclamen
        cls._spawn("free", dict(base, DLLM_HOME=os.path.join(cls.tmp, "f")),
                   ["agent", "--pairing", "--name", "libre", "--port", str(cls.free_agent_port),
                    "--rpc-port", str(free_port()), "--bin-dir", mock_bin, "--backend", "cpu",
                    "--announce-to", "127.0.0.1:9", "--beacon-interval", "5"])
        cls._spawn("orch", dict(base, DLLM_HOME=os.path.join(cls.tmp, "o")),
                   ["up", "--secret", SECRET, "--models-dir", models, "--port", str(cls.op), "--bin-dir", mock_bin,
                    "--beacon-port", str(cls.bp), "--node-timeout", "4", "--probe-interval", "0.5"])
        try:
            cls.wait(lambda: any(n["up"] for n in cls.api("GET", "/api/state")[1]["nodes"]), 40, "nodo visible")
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def _spawn(cls, name, env, args):
        log = open(os.path.join(cls.tmp, f"{name}.log"), "w")
        cls.procs.append(subprocess.Popen([sys.executable, "-m", "dllm"] + args, cwd=ROOT, env=env, stdout=log,
                                          stderr=subprocess.STDOUT, start_new_session=True))

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
        c = http.client.HTTPConnection("127.0.0.1", cls.op, timeout=60)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        d = r.read()
        c.close()
        return r.status, json.loads(d or b"{}")

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
        logs = "\n".join(f"--- {f}\n{open(os.path.join(cls.tmp, f)).read()[-1500:]}"
                         for f in sorted(os.listdir(cls.tmp)) if f.endswith(".log"))
        raise AssertionError(f"timeout esperando: {what}\n{logs}")

    def names(self):
        return {n["name"]: n for n in self.api("GET", "/api/state")[1]["nodes"]}

    def test_1_scan_finds_agents_by_ip(self):
        st, r = self.api("POST", "/api/nodes/scan", {"range": "127.0.0.1-127.0.0.1", "port": self.free_agent_port})
        self.assertEqual(st, 200, r)
        self.wait(lambda: self.api("GET", "/api/nodes/scan")[1].get("state") == "done", 20, "búsqueda")
        job = self.api("GET", "/api/nodes/scan")[1]
        self.assertEqual([(f["host"], f["state"]) for f in job["found"]], [("127.0.0.1", "libre")])
        self.api("POST", "/api/nodes/scan", {"range": "127.0.0.1-127.0.0.1", "port": self.hidden_port})
        self.wait(lambda: self.api("GET", "/api/nodes/scan")[1].get("state") == "done", 20, "búsqueda 2")
        self.assertEqual(self.api("GET", "/api/nodes/scan")[1]["found"][0]["state"], "otro")  # emparejado, aún no en la lista
        self.assertEqual(self.api("POST", "/api/nodes/scan", {"range": "8.8.8.0/24"})[0], 400)

    def test_2_add_by_ip_and_forget(self):
        self.assertNotIn("hidden", self.names())
        st, r = self.api("POST", "/api/nodes/add", {"host": "127.0.0.1", "port": self.hidden_port})
        self.assertEqual((st, r["state"]), (200, "añadido"), r)
        self.wait(lambda: self.names().get("hidden", {}).get("up"), 20, "nodo añadido en línea")
        self.assertEqual(self.api("POST", "/api/nodes/add", {"host": "127.0.0.1", "port": self.hidden_port})[0], 400)
        self.assertEqual(self.api("POST", "/api/nodes/add", {"host": "bad host!"})[0], 400)
        st, r = self.api("POST", "/api/nodes/add", {"host": "127.0.0.1", "port": free_port()})
        self.assertEqual(st, 400)
        self.assertIn("no responde", r["error"])
        nid = self.names()["hidden"]["id"]
        st, r = self.api("POST", "/api/nodes/forget", {"node": nid})
        self.assertEqual(st, 200, r)
        self.assertNotIn("hidden", self.names())
        self.assertEqual(self.api("POST", "/api/nodes/forget", {"node": nid})[0], 400)

    def test_3_add_free_agent_starts_pairing(self):
        st, r = self.api("POST", "/api/nodes/add", {"host": "127.0.0.1", "port": self.free_agent_port})
        self.assertEqual((st, r["state"]), (200, "reclamando"), r)
        self.assertRegex(r["sas"], r"^\d{3} \d{3}$")
        claims = self.api("GET", "/api/state")[1]["claims"]
        self.assertTrue(any(c["host"] == "127.0.0.1" and c["state"].startswith("esperando") for c in claims))

    def test_4_bench_job_and_state(self):
        st, r = self.api("POST", "/api/nodes/bench", {})
        self.assertEqual(st, 200, r)
        self.wait(lambda: self.api("GET", "/api/nodes/bench")[1].get("state") in ("done", "error"), 40, "medición")
        job = self.api("GET", "/api/nodes/bench")[1]
        self.assertEqual(job["state"], "done", job)
        self.assertIn("vis", [i["node"] for i in job["items"]])
        self.assertEqual(self.api("GET", "/api/state")[1]["jobs"]["bench"]["state"], "done")

    def test_5_slow_confirmation_api_and_models_per_node(self):
        nid = self.names()["vis"]["id"]
        self.assertEqual(self.api("POST", "/api/node/settings", {"node": nid, "allow_slow": True})[0], 400)
        st, r = self.api("POST", "/api/node/settings", {"node": nid, "allow_slow": True, "confirm": True})
        self.assertEqual(st, 200, r)
        self.assertTrue(self.names()["vis"]["settings"]["allow_slow"])
        self.api("POST", "/api/node/settings", {"node": nid, "allow_slow": False})
        st, r = self.api("POST", "/api/load", {"model": "small.gguf", "ctx": 2048, "slot": 0})
        self.assertEqual(st, 202, r)
        self.wait(lambda: self.api("GET", "/api/state")[1]["state"] == "READY", 60, "modelo cargado")
        vis = self.names()["vis"]
        self.assertEqual([m["model"] for m in vis["models"]], ["small.gguf"])
        self.assertEqual(vis["models"][0]["device"], "cpu")
        self.assertEqual(self.api("POST", "/api/nodes/forget", {"node": vis["id"]})[0], 409)   # en uso
        self.api("POST", "/api/unload", {})
        self.wait(lambda: self.api("GET", "/api/state")[1]["state"] == "IDLE", 30, "descargado")


if __name__ == "__main__":
    unittest.main(verbosity=2)
