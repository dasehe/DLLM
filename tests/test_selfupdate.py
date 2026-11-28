# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Actualizar DLLM en los nodos desde el orquestador: verificación, sustitución, vuelta atrás y prueba real con un
agente y un orquestador ejecutados desde dllm.pyz de versiones distintas (binarios simulados)."""
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipapp
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]
import _proc  # noqa: E402
from dllm import __version__, selfupdate  # noqa: E402
from ports import free_port  # noqa: E402

SECRET = "s" * 32


def make_pyz(version, dest):
    """dllm.pyz construido desde este código con otra versión."""
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "src"
        shutil.copytree(Path(ROOT) / "dllm", stage / "dllm", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        init = stage / "dllm" / "__init__.py"
        init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init.read_text()))
        zipapp.create_archive(stage, dest, interpreter="/usr/bin/env python3", main="dllm.cli:main")
    return Path(dest)


class SelfUpdateUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.new = make_pyz("9.9.9", cls.tmp / "new.pyz").read_bytes()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=self.tmp))
        self.pyz = self.home / "dllm.pyz"
        self.pyz.write_bytes(b"#!vieja")

    def test_instala_verifica_y_vuelve_atras(self):
        sha = hashlib.sha256(self.new).hexdigest()
        selfupdate.install(self.new, sha, "9.9.9", self.home, "1.0.0", pyz=self.pyz)
        self.assertEqual(self.pyz.read_bytes(), self.new)
        self.assertEqual((self.home / "dllm.pyz.bak").read_bytes(), b"#!vieja")
        m = selfupdate.read_marker(self.home)
        self.assertEqual((m["from"], m["to"], m["attempts"]), ("1.0.0", "9.9.9", 0))
        self.assertTrue(selfupdate.has_backup(self.pyz))
        selfupdate.rollback(self.home)
        self.assertEqual(self.pyz.read_bytes(), b"#!vieja")
        self.assertIsNone(selfupdate.read_marker(self.home))
        with self.assertRaises(selfupdate.UpdateError):
            selfupdate.rollback(self.home, pyz=self.pyz)  # ya no hay copia anterior

    def test_rechazos_sin_tocar_nada(self):
        sha = hashlib.sha256(self.new).hexdigest()
        for data, s, v in ((self.new, "0" * 64, "9.9.9"),          # sha256 distinto
                           (b"basura", hashlib.sha256(b"basura").hexdigest(), "9.9.9"),
                           (self.new, sha, "8.8.8"),               # no es la versión que dice
                           (self.new, sha, "../x")):
            with self.assertRaises(selfupdate.UpdateError):
                selfupdate.install(data, s, v, self.home, "1.0.0", pyz=self.pyz)
            self.assertEqual(self.pyz.read_bytes(), b"#!vieja")
            self.assertFalse((self.home / "dllm.pyz.new").exists())
        with mock.patch.object(selfupdate, "current_pyz", return_value=None), \
                self.assertRaises(selfupdate.UpdateError):  # ejecutado desde el código fuente: no se actualiza
            selfupdate.install(self.new, sha, "9.9.9", self.home, "1.0.0")

    def test_vuelta_atras_automatica_si_no_arranca(self):
        sha = hashlib.sha256(self.new).hexdigest()
        selfupdate.install(self.new, sha, "9.9.9", self.home, "1.0.0", pyz=self.pyz)
        with mock.patch.object(selfupdate, "restart") as rs, mock.patch.object(selfupdate, "HEALTHY_S", 3600):
            for _ in range(selfupdate.MAX_ATTEMPTS):
                selfupdate.startup_guard(self.home, log=lambda *a: None)
            self.assertEqual(self.pyz.read_bytes(), self.new)   # aún dentro de los intentos
            selfupdate.startup_guard(self.home, log=lambda *a: None)
            self.assertEqual(self.pyz.read_bytes(), b"#!vieja")  # demasiados reinicios: versión anterior
            rs.assert_called_once()


@unittest.skipIf(os.name == "nt", "binarios simulados POSIX")
class SelfUpdateE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "models"))
        os.makedirs(os.path.join(cls.tmp, "nodo"))
        cls.old = make_pyz("0.0.1", Path(cls.tmp, "nodo", "dllm.pyz"))
        cls.orch = make_pyz(__version__, Path(cls.tmp, "orq.pyz"))
        cls.op, bp = free_port(), free_port()
        mock_bin = os.path.join(HERE, "mockbin")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        cls.procs = []
        cls.logs = []

        def spawn(name, pyz, home, args):
            log = open(os.path.join(cls.tmp, name + ".log"), "w")
            cls.logs.append(log)
            cls.procs.append(subprocess.Popen([sys.executable, str(pyz)] + args, cwd=cls.tmp,
                                              env=dict(env, DLLM_HOME=os.path.join(cls.tmp, home)),
                                              stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        spawn("nodo", cls.old, "hn", ["agent", "--secret", SECRET, "--name", "n1", "--port", str(free_port()),
                                      "--rpc-port", str(free_port()), "--bin-dir", mock_bin, "--backend", "cpu",
                                      "--mem-gb", "4", "--announce-to", f"127.0.0.1:{bp}", "--beacon-interval", "0.5"])
        spawn("orq", cls.orch, "ho", ["up", "--secret", SECRET, "--models-dir", os.path.join(cls.tmp, "models"),
                                      "--port", str(cls.op), "--bin-dir", mock_bin, "--beacon-port", str(bp),
                                      "--ollama-port", "0", "--probe-interval", "0.5"])
        cls.wait_for(lambda: cls.node().get("version") == "0.0.1", 40, "nodo 0.0.1")

    @classmethod
    def tearDownClass(cls):
        for sig in (signal.SIGTERM, _proc.SIGKILL):
            for p in cls.procs:
                try:
                    _proc.killpg(p.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(0.5)
        for f in cls.logs:
            f.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def api(cls, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.op, timeout=120)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        d = r.read()
        c.close()
        return r.status, json.loads(d or b"{}")

    @classmethod
    def node(cls):
        try:
            return next(iter(cls.api("GET", "/api/state")[1]["nodes"]), {})
        except (OSError, ValueError):
            return {}

    @classmethod
    def wait_for(cls, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.5)
        logs = "".join(open(os.path.join(cls.tmp, n + ".log")).read()[-1500:] for n in ("nodo", "orq"))
        raise AssertionError(f"tiempo agotado: {what}\n{logs}")

    def test_actualiza_y_vuelve_atras(self):
        n = self.node()
        self.assertTrue(n["pyz"])
        st, r = self.api("POST", "/api/nodes/update-dllm", {"node": "all"})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["results"] and r["results"][0]["ok"], r)
        self.wait_for(lambda: self.node().get("version") == __version__, 60, "nodo actualizado")
        self.assertEqual(Path(self.tmp, "nodo", "dllm.pyz").read_bytes(), self.orch.read_bytes())
        self.assertTrue(self.node()["dllm_backup"])
        # nada que hacer con todos ya en la misma versión
        self.assertEqual(self.api("POST", "/api/nodes/update-dllm", {"node": "all"})[1]["results"], [])
        st, r = self.api("POST", "/api/nodes/rollback-dllm", {"node": n["id"]})
        self.assertEqual(st, 200, r)
        self.wait_for(lambda: self.node().get("version") == "0.0.1", 60, "nodo de vuelta a 0.0.1")
        # desactivado por nodo desde el panel
        self.api("POST", "/api/node/settings", {"node": n["id"], "dllm_update": False})
        r = self.api("POST", "/api/nodes/update-dllm", {"node": n["id"]})[1]
        self.assertFalse(r["results"][0]["ok"])
        ev = self.api("GET", "/api/events?q=DLLM")[1]["events"]
        self.assertTrue(any("0.0.1" in e["msg"] for e in ev))


if __name__ == "__main__":
    unittest.main()
