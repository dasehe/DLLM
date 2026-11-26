# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Usuarios, grupos, roles, sesiones y API keys: lógica y orquestador real con login activado (binarios simulados)."""
import http.client
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import _proc  # noqa: E402
from dllm import users  # noqa: E402
from ports import free_port  # noqa: E402


class UsersLogic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.u = users.Users(Path(self.tmp) / "users.json", iterations=1000)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_admin_por_defecto_y_protecciones(self):
        self.assertTrue(self.u.authenticate("admin", "admin"))
        self.assertTrue(self.u.is_default_password("admin"))
        self.assertEqual(self.u.perms_of("admin"), set(users.PERMS))
        for f in (lambda: self.u.delete_user("admin"), lambda: self.u.update_user("admin", disabled=True),
                  lambda: self.u.update_user("admin", groups=["users"]),
                  lambda: self.u.delete_group("administrators"), lambda: self.u.delete_role("administrator"),
                  lambda: self.u.set_role("administrator", ["view"]),
                  lambda: self.u.set_group("administrators", ["lectura"])):
            with self.assertRaises(users.UserError):
                f()
        self.u.init_admin("una-clave-larga")
        self.assertFalse(self.u.is_default_password("admin"))
        self.assertFalse(self.u.authenticate("admin", "admin"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(self.u.path).st_mode), 0o600)
        self.assertNotIn("una-clave-larga", self.u.path.read_text())

    def test_roles_grupos_usuarios(self):
        self.u.set_role("modelista", ["view", "models"])
        self.u.set_group("equipo-ia", ["modelista", "user"])
        self.u.create_user("ana", "contraseña-ana", ["equipo-ia"])
        self.assertEqual(self.u.perms_of("ana"), {"view", "models", "chat"})
        with self.assertRaises(users.UserError):
            self.u.create_user("ana", "otra-clave-12", [])
        with self.assertRaises(users.UserError):
            self.u.create_user("bob", "corta", [])
        with self.assertRaises(users.UserError):
            self.u.create_user("../x", "clave-valida-1", [])
        with self.assertRaises(users.UserError):
            self.u.set_role("raro", ["root"])
        self.u.delete_role("modelista")
        self.assertEqual(self.u.perms_of("ana"), {"view", "chat"})
        self.u.update_user("ana", disabled=True)
        self.assertEqual(self.u.perms_of("ana"), set())
        self.assertFalse(self.u.authenticate("ana", "contraseña-ana"))

    def test_claves_y_sesiones(self):
        self.u.create_user("ana", "contraseña-ana", ["users"])
        kid, key = self.u.create_key("ana", "anythingllm")
        self.assertTrue(key.startswith("dllm_"))
        self.assertNotIn(key, self.u.path.read_text())          # solo el hash en disco
        self.assertEqual(self.u.key_user(key), ("ana", kid))
        self.assertEqual(self.u.key_user("dllm_falsa"), (None, None))
        tok = self.u.new_session("ana")
        self.assertEqual(self.u.session_user(tok), "ana")
        self.u.delete_user("ana")                               # borrar el usuario corta sesiones y claves
        self.assertIsNone(self.u.session_user(tok))
        self.assertEqual(self.u.key_user(key), (None, None))
        kid, key = self.u.create_key("admin", "vieja", key="clave-de-config-json")  # API key anterior
        self.assertEqual(self.u.key_user("clave-de-config-json")[0], "admin")
        self.u.sessions["x"] = {"user": "admin", "exp": time.time() - 1, "ip": ""}
        self.assertIsNone(self.u.session_user("x"))


@unittest.skipIf(os.name == "nt", "binarios simulados POSIX")
class LoginE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "models"))
        cls.home = os.path.join(cls.tmp, "o")
        cls.op = free_port()
        env = dict(os.environ, PYTHONPATH=ROOT, DLLM_HOME=cls.home)
        env.pop("DLLM_INSECURE_NO_LOGIN", None)  # aquí el login va de verdad
        log = open(os.path.join(cls.tmp, "o.log"), "w")
        cls.p = subprocess.Popen([sys.executable, "-m", "dllm", "up", "--secret", "s" * 32, "--models-dir",
                                  os.path.join(cls.tmp, "models"), "--port", str(cls.op), "--beacon-port",
                                  str(free_port()), "--bin-dir", os.path.join(HERE, "mockbin"), "--ollama-port", "0"],
                                 cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        end = time.time() + 30
        while time.time() < end:
            try:
                if cls.req("GET", "/healthz")[0] == 200:
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
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, path, body=None, cookie=None, bearer=None, headers=None):
        h = dict(headers or {})
        h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = cookie
        if bearer:
            h["Authorization"] = "Bearer " + bearer
        c = http.client.HTTPConnection("127.0.0.1", cls.op, timeout=30)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            j = json.loads(data) if data else {}
        except ValueError:
            j = {}
        return r.status, j, r.getheader("Set-Cookie") or ""

    def login(self, user, pw):
        st, j, ck = self.req("POST", "/api/login", {"user": user, "password": pw})
        return st, j, ck.split(";")[0]

    def test_1_sin_sesion(self):
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/api/users")[0], 401)
        self.assertEqual(self.req("GET", "/v1/models")[0], 200)  # aplicaciones del propio equipo, como antes
        self.assertEqual(self.req("GET", "/")[0], 200)            # la página (con el formulario de login)

    def test_2_admin_por_defecto_y_usuarios(self):
        st, me, ck = self.login("admin", "admin")
        self.assertEqual(st, 200, me)
        self.assertTrue(me["default_pw"])
        self.assertIn("HttpOnly", self.req("POST", "/api/login", {"user": "admin", "password": "admin"})[2])
        self.assertEqual(self.req("GET", "/api/state", cookie=ck)[0], 200)
        st, v, _ = self.req("POST", "/api/users/create", {"name": "ana", "password": "contraseña-ana",
                                                           "groups": ["users"]}, cookie=ck)
        self.assertEqual(st, 200, v)
        self.assertEqual(self.req("POST", "/api/users/delete", {"name": "admin"}, cookie=ck)[0], 400)
        st, me, cka = self.login("ana", "contraseña-ana")
        self.assertEqual((st, me["perms"]), (200, ["chat", "view"]))
        self.assertEqual(self.req("GET", "/api/state", cookie=cka)[0], 200)
        st, j, _ = self.req("POST", "/api/unload", {}, cookie=cka)
        self.assertEqual(st, 403)
        self.assertEqual(self.req("GET", "/api/users", cookie=cka)[0], 403)
        self.assertEqual(self.req("GET", "/api/join-code", cookie=cka)[0], 403)
        # clave propia: hereda los permisos de ana
        st, k, _ = self.req("POST", "/api/me/keys", {"name": "script"}, cookie=cka)
        self.assertEqual(st, 200, k)
        self.assertEqual(self.req("GET", "/api/state", bearer=k["key"])[0], 200)
        self.assertEqual(self.req("POST", "/api/unload", {}, bearer=k["key"])[0], 403)
        # cerrar sesión invalida la cookie
        self.req("POST", "/api/logout", {}, cookie=cka)
        self.assertEqual(self.req("GET", "/api/state", cookie=cka)[0], 401)
        # cambiar la contraseña de admin quita el aviso
        st, me, _ = self.req("POST", "/api/me/password", {"old": "admin", "new": "nueva-clave-admin"}, cookie=ck)
        self.assertEqual((st, me["default_pw"]), (200, False))
        self.assertEqual(self.login("admin", "admin")[0], 401)
        self.assertEqual(self.login("admin", "nueva-clave-admin")[0], 200)

    def test_3_token_local_y_bloqueo(self):
        tok = Path(self.home, "local.token").read_text().strip()
        self.assertEqual(self.req("GET", "/api/state", bearer=tok)[0], 200)   # `dllm status` en este equipo
        for _ in range(5):
            self.assertEqual(self.login("nadie", "mal")[0], 401)
        st, j, _ = self.login("nadie", "mal")
        self.assertEqual(st, 429)
        st, ev, _ = self.req("GET", "/api/events?q=fallido", bearer=tok)
        self.assertTrue(any("fallido" in e["msg"] for e in ev["events"]))


if __name__ == "__main__":
    unittest.main()
