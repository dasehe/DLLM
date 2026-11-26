# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Desinstalador web separado (plan, copia previa, protecciones) y restauración desde copia en el instalador.
Servidor simulado en proceso; las acciones destructivas de installer.uninstall_all se sustituyen por un doble."""
import base64
import json
import os
import secrets
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from dllm import backup, installer, wizard  # noqa: E402
from dllm.util import FastHTTPServer  # noqa: E402
from http.server import BaseHTTPRequestHandler  # noqa: E402
from test_backup import CFG, SECRET, make_home  # noqa: E402


class Web:
    def __init__(self, mode, dry=False):
        self.tok = secrets.token_urlsafe(8)
        self.srv = FastHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.port = self.srv.server_address[1]
        self.st = wizard._WebState(self.tok, self.port, dry, mode)
        self.srv.RequestHandlerClass = wizard._make_web_handler(self.st)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, path, body=None, method=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}?t={self.tok}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method=method or ("POST" if body is not None else "GET"),
                                     headers={"X-Setup-Token": self.tok})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers["Content-Type"].startswith("application/json") else raw.decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class UninstallWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home, self.out = t / "home", t / "out"
        self.out.mkdir()
        self.env = mock.patch.dict(os.environ, {"DLLM_HOME": str(self.home)})
        self.env.start()
        self.fake = mock.patch.object(installer, "uninstall_all", return_value=["simulado"])
        self.web = None

    def tearDown(self):
        if self.web:
            self.web.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_pagina_propia_y_sin_instalar(self):
        self.web = Web("uninstall")
        code, html = self.web.call("/")
        self.assertEqual(code, 200)
        self.assertIn("Desinstalar DLLM", html)
        self.assertNotIn("__TOKEN__", html)
        self.assertEqual(self.web.call("/api/inspect")[1], {"installed": False})

    def test_instalador_ya_no_desinstala(self):
        self.web = Web("install")
        self.assertNotIn("uninstall(", self.web.call("/")[1])
        code, r = self.web.call("/api/uninstall", {"purge": True})
        self.assertEqual(code, 400)
        self.assertIn("programa", r["error"])

    def test_inspect_con_instalacion(self):
        make_home(self.home, dict(CFG, install_dir=str(self.out), models_dir=str(self.out / "models")))
        (self.out / "models").mkdir()
        (self.out / "models" / "a.gguf").write_bytes(b"x" * 10)
        self.web = Web("uninstall")
        with self.fake:
            code, r = self.web.call("/api/inspect")
        self.assertTrue(r["installed"] and r["can_backup"] and r["models_own"])
        self.assertEqual((r["models_count"], r["models_bytes"]), (1, 10))
        self.assertEqual(r["plan_all"], ["simulado"])

    def test_copia_previa_cifrada_y_restauracion(self):
        make_home(self.home, dict(CFG, install_dir=str(self.out)))
        self.web = Web("uninstall")
        with self.fake as f:
            code, r = self.web.call("/api/uninstall", {"purge": True, "backup": True, "backup_dir": str(self.out),
                                                       "backup_pass": "clave-larga-123"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["done"], ["simulado"])
        f.assert_called_once()
        p = Path(r["backup"])
        self.assertTrue(p.exists() and p.parent == self.out)
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        blob = p.read_bytes()
        self.assertTrue(backup.is_encrypted(blob))
        with self.assertRaises(backup.BackupError):
            backup.open_backup(blob, "otra")
        man, files = backup.open_backup(blob, "clave-larga-123")
        self.assertEqual(json.loads(files["config.json"])["secret"], SECRET)

    def test_protecciones_de_la_copia(self):
        make_home(self.home, dict(CFG, install_dir=str(self.out)))
        self.web = Web("uninstall")
        with self.fake as f:
            c, r = self.web.call("/api/uninstall", {"backup": True, "backup_dir": str(self.home)})
            self.assertEqual(c, 400)
            self.assertIn("fuera", r["error"])
            c, r = self.web.call("/api/uninstall", {"backup": True, "backup_dir": str(self.out / "no-existe")})
            self.assertEqual(c, 400)
            f.assert_not_called()  # un error en la copia aborta: no se desinstala sin la copia pedida

    def test_nodo_no_tiene_copia(self):
        make_home(self.home, dict(CFG, role="node"))
        self.web = Web("uninstall")
        with self.fake as f:
            c, r = self.web.call("/api/uninstall", {"backup": True, "backup_dir": str(self.out)})
        self.assertEqual(c, 400)
        f.assert_not_called()

    def test_simulacion_no_escribe(self):
        make_home(self.home, dict(CFG, install_dir=str(self.out)))
        self.web = Web("uninstall", dry=True)
        with self.fake:
            c, r = self.web.call("/api/uninstall", {"backup": True, "backup_dir": str(self.out)})
        self.assertEqual(c, 200)
        self.assertEqual(list(self.out.iterdir()), [])


class RestoreInInstaller(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "los binarios simulados de llama.cpp son scripts sin .exe")
    def test_restaurar_en_instalacion_nueva(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            old = t / "viejo"
            make_home(old)
            blob = backup.build(old, host="viejo", passphrase="pw-restaurar-1")
            r = {"data": base64.b64encode(blob).decode(), "pass": "pw-restaurar-1"}
            new = t / "nuevo"
            with mock.patch.dict(os.environ, {"DLLM_HOME": str(new)}):
                w = Web("install")
                try:
                    c, v = w.call("/api/restore/preview", r)
                    self.assertEqual(c, 200, v)
                    self.assertTrue(v["has_secret"] and v["encrypted"])
                    c, v = w.call("/api/restore/preview", dict(r, **{"pass": "mal"}))
                    self.assertEqual(c, 400)
                    c, v = w.call("/api/restore/preview", {"data": "!!no-base64!!"})
                    self.assertEqual(c, 400)
                finally:
                    w.close()
                # instalación real (sin red: llama.cpp simulado con los binarios de prueba, sin servicio)
                s = wizard.Setup()
                res = s.install({"role": "orchestrator", "name": "nueva", "install_dir": str(t / "DLLM"),
                                 "bin_dir": os.path.join(HERE, "mockbin"), "autostart": False, "firewall": False,
                                 "restore": r, "admin_password": "clave-admin-123"})
                cfg = json.loads((new / "config.json").read_text())
                self.assertEqual(cfg["secret"], SECRET)  # secreto del clúster recuperado
                self.assertEqual(cfg["orchestrator"]["api_key"], "api-key-vieja")
                self.assertEqual(cfg["name"], "nueva")  # rutas y nombre: los de esta máquina
                self.assertEqual(cfg["install_dir"], str(t / "DLLM"))
                self.assertEqual(res["api_key"], "api-key-vieja")
                self.assertTrue((new / "models.json").exists() and (new / "nodes.json").exists())
                from dllm import users
                um = users.Users(new / "users.json")
                self.assertTrue(um.authenticate("admin", "clave-admin-123"))  # contraseña de admin del instalador
                self.assertFalse(um.is_default_password("admin"))

    def test_copia_en_nodo_rechazada(self):
        with tempfile.TemporaryDirectory() as t:
            old = Path(t) / "v"
            make_home(old)
            r = {"data": base64.b64encode(backup.build(old)).decode()}
            with mock.patch.dict(os.environ, {"DLLM_HOME": str(Path(t) / "n")}):
                with self.assertRaises(ValueError):
                    wizard.Setup().install({"role": "node", "install_dir": str(Path(t) / "D"), "restore": r,
                                            "bin_dir": os.path.join(HERE, "mockbin"), "autostart": False})


if __name__ == "__main__":
    unittest.main()
