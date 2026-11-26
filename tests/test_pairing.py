# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Emparejamiento de nodos instalados sin código de unión: criptografía y flujo completo con procesos reales."""
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
from dllm import pairing  # noqa: E402
from ports import free_port  # noqa: E402


class TestCrypto(unittest.TestCase):
    def test_dh_sas_and_seal(self):
        xa, pa = pairing.keypair()
        xb, pb = pairing.keypair()
        ka, kb = pairing.shared_key(xa, str(pb)), pairing.shared_key(xb, str(pa))
        self.assertEqual(ka, kb)
        self.assertEqual(pairing.sas(ka, pa, pb), pairing.sas(kb, pa, pb))
        self.assertRegex(pairing.sas(ka, pa, pb), r"^\d{3} \d{3}$")
        box = pairing.seal(ka, {"secret": "x" * 40})
        self.assertNotIn("xxxx", json.dumps(box))
        self.assertEqual(pairing.open_sealed(kb, box)["secret"], "x" * 40)
        # un intermediario con otra clave no puede leerlo ni modificarlo
        xm, pm = pairing.keypair()
        with self.assertRaises(ValueError):
            pairing.open_sealed(pairing.shared_key(xm, str(pa)), box)
        box["ct"] = ("0" if box["ct"][0] != "0" else "1") + box["ct"][1:]
        with self.assertRaises(ValueError):
            pairing.open_sealed(kb, box)
        for bad in ("1", "0", str(pairing.P - 1), str(pairing.P), "x"):
            with self.assertRaises(ValueError):
                pairing.shared_key(xa, bad)

    def test_beacon_parsing(self):
        b = pairing.beacon("vDSH2", 8765, "abcd1234")
        self.assertEqual(pairing.parse_beacon(b)["name"], "vDSH2")
        for bad in (b"DLLMPAIR1 {}", b"DLLMPAIR1 no-json", b"otra cosa",
                    pairing.beacon("<script>", 8765, "abcd1234"), pairing.beacon("x", 70000, "abcd1234")):
            self.assertIsNone(pairing.parse_beacon(bad))

    def test_node_requires_local_accept(self):
        got = []
        node = pairing.NodePairing(got.append)
        x, pub = pairing.keypair()
        r = node.request("10.0.0.5", {"orch": "DSH", "pub": str(pub)})
        key = pairing.shared_key(x, r["pub"])
        box = pairing.seal(key, {"secret": "s" * 43})
        with self.assertRaises(PermissionError):  # aún no aceptado en el nodo
            node.complete("10.0.0.5", {"id": r["id"], "box": box})
        with self.assertRaises(PermissionError):  # otro equipo no puede adelantarse
            node.request("10.0.0.6", {"orch": "intruso", "pub": str(pub)})
        self.assertEqual(node.local_view()["sas"], pairing.sas(key, pub, r["pub"]))
        node.decide(r["id"], True)
        with self.assertRaises(PermissionError):  # desde otra IP
            node.complete("10.0.0.6", {"id": r["id"], "box": box})
        node.complete("10.0.0.5", {"id": r["id"], "box": box})
        self.assertEqual((got[0]["secret"], got[0]["orch_ip"]), ("s" * 43, "10.0.0.5"))


@unittest.skipIf(os.name == "nt", "usa binarios simulados con shebang y grupos de procesos POSIX")
class TestPairingE2E(unittest.TestCase):
    """Agente sin secreto + orquestador reales: reclamar desde el panel, aceptar en el nodo y verlo unido."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.procs = []
        mock = os.path.join(HERE, "mockbin")
        cls.bp, cls.op, cls.ap = free_port(), free_port(), free_port()
        home = os.path.join(cls.tmp, "node")
        os.makedirs(home)
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"role": "node", "name": "nuevo", "secret": None, "agent": {}, "orchestrator": {}}, f)
        env = dict(os.environ, PYTHONPATH=ROOT, DLLM_HOME=home)
        cls.node_home = home
        cls._spawn(["agent", "--pairing", "--name", "nuevo", "--port", str(cls.ap), "--rpc-port", str(free_port()),
                    "--bin-dir", mock, "--backend", "cpu", "--mem-gb", "4", "--announce-to", f"127.0.0.1:{cls.bp}",
                    "--beacon-port", str(cls.bp), "--beacon-interval", "0.5",
                    "--config-file", os.path.join(home, "config.json")], env)
        models = os.path.join(cls.tmp, "models")
        os.makedirs(models)
        cls._spawn(["up", "--secret", "secreto-del-cluster-de-prueba-1234", "--models-dir", models,
                    "--port", str(cls.op), "--bin-dir", mock, "--beacon-port", str(cls.bp),
                    "--node-timeout", "3", "--probe-interval", "0.5"],
                   dict(os.environ, PYTHONPATH=ROOT, DLLM_HOME=os.path.join(cls.tmp, "orch")))

    @classmethod
    def _spawn(cls, args, env):
        log = open(os.path.join(cls.tmp, f"p{len(cls.procs)}.log"), "w")
        cls.procs.append(subprocess.Popen([sys.executable, "-m", "dllm"] + args, env=env, cwd=ROOT, stdout=log,
                                          stderr=subprocess.STDOUT, start_new_session=True))

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            try:
                _proc.killpg(p.pid, _proc.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def http(self, port, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        d = json.loads(r.read() or b"{}")
        c.close()
        return r.status, d

    def wait(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = cond()
                if v:
                    return v
            except OSError:
                pass
            time.sleep(0.3)
        logs = "".join(open(os.path.join(self.tmp, f)).read()[-2000:] for f in sorted(os.listdir(self.tmp))
                       if f.endswith(".log"))
        self.fail(f"timeout: {what}\n{logs}")

    def test_claim_confirm_join(self):
        st = self.wait(lambda: self.http(self.op, "GET", "/api/state")[1].get("pending"), 30, "nodo pendiente")
        self.assertEqual(st[0]["name"], "nuevo")
        # sin emparejar, el agente no atiende órdenes ni túneles
        self.assertEqual(self.http(self.ap, "GET", "/info")[0], 503)
        code, r = self.http(self.op, "POST", "/api/pair/claim", {"host": "127.0.0.1"})
        self.assertEqual(code, 200, r)
        v = self.wait(lambda: (lambda d: d if d.get("state") == "pendiente" else None)(
            self.http(self.ap, "GET", "/pair/local")[1]), 10, "solicitud en el nodo")
        self.assertEqual(v["sas"], r["sas"])  # el mismo código en el panel y en el nodo
        self.assertEqual(self.http(self.ap, "POST", "/pair/local", {"id": v["id"], "accept": True})[0], 200)
        self.wait(lambda: any(n["up"] and n["name"] == "nuevo" for n in self.http(self.op, "GET", "/api/state")[1]
                              ["nodes"]), 30, "nodo unido y activo")
        with open(os.path.join(self.node_home, "config.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["secret"], "secreto-del-cluster-de-prueba-1234")
        self.assertEqual(cfg["agent"]["announce_to"], [f"127.0.0.1:{self.bp}"])
        self.assertEqual(self.http(self.ap, "GET", "/pair/local")[0], 404)  # ya no admite más emparejamientos


if __name__ == "__main__":
    unittest.main()
