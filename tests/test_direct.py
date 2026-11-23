# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Un modelo que cabe en la GPU local del orquestador se ejecuta sin RPC, y otro modelo puede seguir cargándose en
los demás nodos. Binarios simulados: el orquestador y el agente «amd» comparten equipo (127.0.0.1)."""
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
SECRET = "secreto-directo-1234567890abcdef"
GPU = "Vulkan0|AMD Radeon RX 7600 XT|16368|15800"


@unittest.skipIf(os.name == "nt", "binarios simulados con shebang y grupos de procesos POSIX")
class DirectLocalGpu(unittest.TestCase):
    NODB = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        models = os.path.join(cls.tmp, "models")
        os.makedirs(models)
        write_gguf(os.path.join(models, "small.gguf"), 2 * GiB)
        write_gguf(os.path.join(models, "other.gguf"), 1 * GiB)
        cls.procs, cls.op, cls.bp = [], free_port(), free_port()
        mb = os.path.join(HERE, "mockbin")
        base = dict(os.environ, PYTHONPATH=ROOT)
        cls.spawn("gpu", dict(base, DLLM_HOME=cls.tmp + "/g", MOCK_DEVICES=GPU),
                  ["agent", "--secret", SECRET, "--name", "gpu", "--port", str(free_port()), "--rpc-port", str(free_port()),
                   "--bin-dir", mb, "--llama-backend", "cpu", "--announce-to", f"127.0.0.1:{cls.bp}",
                   "--beacon-interval", "0.5"])
        cls.spawn("cpu", dict(base, DLLM_HOME=cls.tmp + "/c"),
                  ["agent", "--secret", SECRET, "--name", "cpu", "--port", str(free_port()), "--rpc-port", str(free_port()),
                   "--bin-dir", mb, "--backend", "cpu", "--mem-gb", "8", "--bw-gbps", "30",
                   "--announce-to", f"127.0.0.1:{cls.bp}", "--beacon-interval", "0.5"])
        extra = ["--no-direct-local"] if cls.NODB else []
        cls.spawn("orch", dict(base, DLLM_HOME=cls.tmp + "/o", MOCK_LOCAL_DEVICES=GPU),
                  ["up", "--secret", SECRET, "--models-dir", models, "--port", str(cls.op), "--bin-dir", mb,
                   "--beacon-port", str(cls.bp), "--node-timeout", "4", "--probe-interval", "0.5"] + extra)
        try:
            cls.wait(lambda: len([n for n in cls.api("GET", "/api/state")[1]["nodes"] if n["up"]]) == 2, 40, "2 nodos")
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def spawn(cls, name, env, args):
        log = open(os.path.join(cls.tmp, name + ".log"), "w")
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

    def slot(self, n):
        return next(s for s in self.api("GET", "/api/state")[1]["slots"] if s["slot"] == n)

    def ready(self, n):
        self.wait(lambda: self.slot(n)["state"] in ("READY", "ERROR"), 60, f"slot {n}")
        self.assertEqual(self.slot(n)["state"], "READY", self.slot(n)["error"])

    def chat(self, model):
        st, r = self.api("POST", "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual(st, 200, r)
        return r["mock"]

    def test_direct_then_second_model_on_other_nodes(self):
        st, r = self.api("POST", "/api/load", {"model": "small.gguf", "ctx": 2048, "slot": 0,
                                               "targets": [{"node": "gpu", "device": "gpu"}]})
        self.assertEqual(st, 202, r)
        self.ready(0)
        info = self.chat("small.gguf")
        if self.NODB:
            self.assertTrue(info["rpc"])
            self.assertIsNone(self.slot(0)["direct"])
            return
        self.assertEqual(info["rpc"], [])                      # sin RPC
        self.assertEqual(info["device"], "Vulkan0")            # la GPU local
        self.assertIn("AMD Radeon", self.slot(0)["direct"])
        # otro modelo, a la vez, en el otro nodo (CPU): no queda bloqueado por el directo
        st, r = self.api("POST", "/api/load", {"model": "other.gguf", "ctx": 2048, "slot": "new",
                                               "targets": [{"node": "cpu", "device": "cpu"}]})
        self.assertEqual(st, 202, r)
        k = r["slot"]
        self.ready(k)
        self.assertTrue(self.chat("other.gguf")["rpc"])         # este sí va por RPC
        self.assertEqual(self.chat("small.gguf")["rpc"], [])    # el directo sigue vivo
        # descargar el directo: se libera la reserva y no se llama a /rpc/stop de nadie
        self.api("POST", "/api/unload", {"slot": 0})
        self.wait(lambda: self.slot(0)["state"] == "IDLE", 30, "descarga")
        self.assertIsNone(self.slot(0)["direct"])


class DirectDisabled(DirectLocalGpu):
    NODB = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
