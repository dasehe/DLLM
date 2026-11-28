# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Alertas por webhook contra un receptor HTTP local: firma HMAC, formatos, filtros, sin redirecciones, reintentos,
límite por minuto, validación y secretos fuera de las vistas."""
import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dllm import webhooks  # noqa: E402


class Receiver:
    """Servidor HTTP local que guarda lo recibido; `codes` = respuestas a dar en orden (luego 200)."""

    def __init__(self):
        self.got, self.codes = [], []
        rec = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                rec.got.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                code = rec.codes.pop(0) if rec.codes else 200
                self.send_response(code)
                if code in (301, 302, 307, 308):
                    self.send_header("Location", "http://127.0.0.1:9/otro")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def wait(self, n, t=5):
        end = time.time() + t
        while len(self.got) < n and time.time() < end:
            time.sleep(0.02)
        return self.got


class WebhooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rx = Receiver()
        self.w = webhooks.Webhooks(os.path.join(self.tmp, "webhooks.json"), origin="orq", timeout=3,
                                   retries=(0.05, 0.05))

    def tearDown(self):
        self.rx.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_firma_y_formato_generico(self):
        hid, sec = self.w.set({"name": "n8n", "url": self.rx.url + "/hook"})
        self.assertTrue(sec and len(sec) > 30)
        r = self.w.test(hid)
        self.assertEqual((r["ok"], r["code"]), (True, 200))
        path, h, body = self.rx.got[0]
        self.assertEqual(path, "/hook")
        exp = "sha256=" + hmac.new(sec.encode(), (h["x-dllm-timestamp"] + ".").encode() + body,
                                   hashlib.sha256).hexdigest()
        self.assertEqual(h["x-dllm-signature"], exp)
        self.assertEqual(h["x-dllm-event"], "test")
        d = json.loads(body)
        self.assertEqual((d["source"], d["origin"], d["event"]["kind"]), ("dllm", "orq", "test"))
        # secreto: en disco con 600, nunca en la vista
        v = json.dumps(self.w.view())
        self.assertNotIn(sec, v)
        self.assertIn(sec, open(self.w.path, encoding="utf-8").read())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.w.path).st_mode), 0o600)
        self.assertEqual(self.w.view()["hooks"][hid]["status"]["sent"], 1)
        # el secreto se regenera bajo petición y la firma cambia
        _, sec2 = self.w.set({"id": hid, "regenerate": True})
        self.assertNotEqual(sec, sec2)
        self.assertIsNone(self.w.set({"id": hid, "name": "otro"})[1])

    def test_formatos_slack_discord(self):
        a, _ = self.w.set({"url": self.rx.url + "/s", "format": "slack"})
        b, _ = self.w.set({"url": self.rx.url + "/d", "format": "discord"})
        self.w.test(a)
        self.w.test(b)
        self.assertIn("text", json.loads(self.rx.got[0][2]))
        self.assertIn("content", json.loads(self.rx.got[1][2]))

    def test_filtros_y_envio_en_segundo_plano(self):
        self.w.set({"url": self.rx.url + "/err", "min_level": "error"})
        self.w.set({"url": self.rx.url + "/node", "min_level": "info", "kinds": "node, security"})
        self.w.set({"url": self.rx.url + "/off", "enabled": False, "min_level": "info"})
        self.w.emit({"level": "info", "kind": "load", "msg": "cargado"})          # nadie
        self.w.emit({"level": "warn", "kind": "node", "msg": "nodo caído"})       # /node
        self.w.emit({"level": "error", "kind": "load", "msg": "fallo"})           # /err
        got = self.rx.wait(2)
        time.sleep(0.2)
        self.assertEqual(sorted(g[0] for g in got), ["/err", "/node"])

    def test_sin_redirecciones_y_reintentos(self):
        hid, _ = self.w.set({"url": self.rx.url + "/r", "min_level": "info"})
        self.rx.codes = [302]
        r = self.w.test(hid)
        self.assertFalse(r["ok"])
        self.assertEqual(len(self.rx.got), 1)  # no siguió la redirección
        self.rx.codes = [500, 503]             # se reintenta hasta entregar
        self.w.emit({"level": "warn", "kind": "node", "msg": "x"})
        self.assertEqual(len(self.rx.wait(4)), 4)
        self.rx.codes = [404]                   # error del receptor: no se reintenta
        self.w.emit({"level": "warn", "kind": "node", "msg": "y"})
        self.rx.wait(5)
        time.sleep(0.3)
        self.assertEqual(len(self.rx.got), 5)

    def test_limite_por_minuto(self):
        hid, _ = self.w.set({"url": self.rx.url + "/many", "min_level": "info"})
        for i in range(webhooks.PER_MINUTE + 5):
            self.w.emit({"level": "info", "kind": "node", "msg": f"e{i}"})
        self.rx.wait(webhooks.PER_MINUTE)
        time.sleep(0.3)
        self.assertEqual(len(self.rx.got), webhooks.PER_MINUTE)
        self.assertEqual(self.w.view()["hooks"][hid]["status"]["dropped"], 5)

    def test_validacion(self):
        for bad in ({"url": "ftp://x"}, {"url": "http://"}, {"url": "http://u:p@h/"}, {"url": "http://h/\r\nX: y"},
                    {"url": self.rx.url, "format": "xml"}, {"url": self.rx.url, "min_level": "debug"},
                    {"url": self.rx.url, "kinds": ["raro"]}, {"url": self.rx.url, "name": "<script>"},
                    {"id": "noexiste", "name": "x"}):
            with self.assertRaises(webhooks.WebhookError, msg=bad):
                self.w.set(bad)
        with self.assertRaises(webhooks.WebhookError):
            self.w.delete("noexiste")
        hid, _ = self.w.set({"url": self.rx.url})
        self.w.delete(hid)
        self.assertEqual(self.w.view()["hooks"], {})
        r = webhooks.Webhooks(self.w.path)  # persistencia
        self.assertEqual(r.hooks, {})


if __name__ == "__main__":
    unittest.main()
