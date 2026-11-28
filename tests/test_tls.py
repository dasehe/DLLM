# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""HTTPS opcional: certificado autofirmado con openssl, huella, HTTP y HTTPS en el mismo puerto, redirección del HTTP
sin cifrar desde la red, cookie Secure y orquestador real con --tls-self-signed (binarios simulados)."""
import http.client
import json
import os
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]
import _proc  # noqa: E402
from dllm import config, tls  # noqa: E402
from ports import free_port  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None


class Hello(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"tls": isinstance(self.connection, ssl.SSLSocket)}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@unittest.skipUnless(HAVE_OPENSSL, "hace falta openssl")
class TLSUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cert, cls.key = tls.self_signed(cls.tmp / "tls", hosts=["10.1.2.3", "orq.lan"])
        cls.srv = tls.TLSHTTPServer(("127.0.0.1", 0), Hello)
        cls.srv.ssl_ctx = tls.make_context(cls.cert, cls.key)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def client_ctx(self):
        ctx = ssl.create_default_context(cafile=str(self.cert))
        return ctx

    def get(self, conn):
        conn.request("GET", "/x?y=1")
        r = conn.getresponse()
        return r.status, r.read(), r.getheader("Location")

    def test_certificado(self):
        self.assertTrue(self.cert.is_file() and self.key.is_file())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)
        fp = tls.fingerprint(self.cert)
        self.assertRegex(fp, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertEqual(tls.self_signed(self.tmp / "tls"), (self.cert, self.key))  # no se regenera
        with self.assertRaises(tls.TLSError):
            tls.make_context(self.cert, self.tmp / "no-existe.pem")

    def test_https_y_http_local_en_el_mismo_puerto(self):
        st, body, _ = self.get(http.client.HTTPSConnection("127.0.0.1", self.port, context=self.client_ctx(),
                                                           timeout=10))
        self.assertEqual((st, json.loads(body)["tls"]), (200, True))  # el SAN incluye 127.0.0.1: verifica bien
        st, body, _ = self.get(http.client.HTTPConnection("127.0.0.1", self.port, timeout=10))
        self.assertEqual((st, json.loads(body)["tls"]), (200, False))
        with self.assertRaises(ssl.SSLError):  # sin confiar en el certificado, el cliente lo rechaza
            self.get(http.client.HTTPSConnection("127.0.0.1", self.port, context=ssl.create_default_context(),
                                                 timeout=10))
        # el servidor sigue atendiendo después del rechazo
        st, _, _ = self.get(http.client.HTTPConnection("127.0.0.1", self.port, timeout=10))
        self.assertEqual(st, 200)

    def test_http_desde_la_red_redirige(self):
        self.srv.loopback = ()  # simula que 127.0.0.1 es un equipo de la red
        try:
            st, body, loc = self.get(http.client.HTTPConnection("127.0.0.1", self.port, timeout=10))
            self.assertEqual((st, loc), (301, f"https://127.0.0.1:{self.port}/x?y=1"))
            st, body, _ = self.get(http.client.HTTPSConnection("127.0.0.1", self.port, context=self.client_ctx(),
                                                               timeout=10))
            self.assertEqual(st, 200)
        finally:
            self.srv.loopback = tls.LOOPBACK

    def test_orden_dllm_tls(self):
        home = self.tmp / "home"
        home.mkdir()
        cfg = {"secret": "s", "models_dir": "/m", "bin_dir": "/b", "role": "orchestrator",
               "orchestrator": {"host": "0.0.0.0", "port": 8080, "min_mbps": 800, "beacon_port": 50999}}
        (home / "config.json").write_text(json.dumps(cfg))
        env = dict(os.environ, PYTHONPATH=ROOT, DLLM_HOME=str(home), DLLM_LANG="en")
        run = lambda *a: subprocess.run([sys.executable, "-m", "dllm", "tls", *a], env=env, cwd=ROOT,
                                        capture_output=True, text=True, timeout=60)
        r = run("enable")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fingerprint", r.stdout)
        self.assertEqual(json.loads((home / "config.json").read_text())["orchestrator"]["tls"], {"mode": "self-signed"})
        self.assertIn("enabled", run("status").stdout)
        self.assertNotEqual(run("enable", "--cert", str(self.cert)).returncode, 0)  # falta --key
        r = run("enable", "--cert", str(self.cert), "--key", str(self.key))
        self.assertEqual(r.returncode, 0, r.stderr)
        run("disable")
        self.assertNotIn("tls", json.loads((home / "config.json").read_text())["orchestrator"])

    def test_up_argv(self):
        base = {"secret": "s", "models_dir": "/m", "bin_dir": "/b", "role": "orchestrator",
                "orchestrator": {"host": "0.0.0.0", "port": 8080, "min_mbps": 800, "beacon_port": 50999}}
        base["orchestrator"]["tls"] = {"mode": "self-signed"}
        self.assertIn("--tls-self-signed", config.up_argv(base))
        base["orchestrator"]["tls"] = {"cert": "/c.pem", "key": "/k.pem"}
        a = config.up_argv(base)
        self.assertEqual(a[a.index("--tls-cert") + 1], "/c.pem")


@unittest.skipUnless(HAVE_OPENSSL and os.name != "nt", "hace falta openssl (binarios simulados POSIX)")
class TLSOrchestrator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "models"))
        cls.home = os.path.join(cls.tmp, "o")
        cls.op = free_port()
        env = dict(os.environ, PYTHONPATH=ROOT, DLLM_HOME=cls.home)
        env.pop("DLLM_INSECURE_NO_LOGIN", None)
        cls.log = open(os.path.join(cls.tmp, "o.log"), "w")
        cls.p = subprocess.Popen([sys.executable, "-m", "dllm", "up", "--secret", "s" * 32, "--models-dir",
                                  os.path.join(cls.tmp, "models"), "--port", str(cls.op), "--beacon-port",
                                  str(free_port()), "--bin-dir", os.path.join(HERE, "mockbin"), "--ollama-port", "0",
                                  "--tls-self-signed"],
                                 cwd=ROOT, env=env, stdout=cls.log, stderr=subprocess.STDOUT, start_new_session=True)
        cls.cert = os.path.join(cls.home, "tls", "dllm-cert.pem")
        end = time.time() + 40
        while time.time() < end:
            try:
                c = http.client.HTTPConnection("127.0.0.1", cls.op, timeout=5)
                c.request("GET", "/healthz")
                if c.getresponse().status == 200 and os.path.exists(cls.cert):
                    break
            except OSError:
                time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        for sig in (signal.SIGTERM, _proc.SIGKILL):
            try:
                _proc.killpg(cls.p.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            time.sleep(0.5)
        cls.log.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def https(self):
        return http.client.HTTPSConnection("127.0.0.1", self.op, timeout=10,
                                           context=ssl.create_default_context(cafile=self.cert))

    def test_login_por_https_con_cookie_secure(self):
        c = self.https()
        c.request("POST", "/api/login", body=json.dumps({"user": "admin", "password": "admin"}),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        ck = r.getheader("Set-Cookie")
        self.assertIn("Secure", ck)
        c.request("GET", "/api/access", headers={"Cookie": ck.split(";")[0]})
        a = json.loads(c.getresponse().read())
        self.assertEqual(a["tls"]["fingerprint"], tls.fingerprint(self.cert))
        self.assertEqual(a["tls"]["mode"], "self-signed")
        # HTTP desde el propio equipo (terminal): sigue funcionando, sin Secure en la cookie
        p = http.client.HTTPConnection("127.0.0.1", self.op, timeout=10)
        p.request("POST", "/api/login", body=json.dumps({"user": "admin", "password": "admin"}),
                  headers={"Content-Type": "application/json"})
        r = p.getresponse()
        r.read()
        self.assertNotIn("Secure", r.getheader("Set-Cookie"))
        self.assertIn("huella", open(os.path.join(self.tmp, "o.log"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
