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

    def test_limite_por_clave(self):
        kid, key = self.u.create_key("admin", "script", rpm=3)
        t = 1000.0
        self.assertEqual([self.u.allow(kid, t + i)[0] for i in range(4)], [True, True, True, False])
        ok, wait = self.u.allow(kid, t + 10)
        self.assertFalse(ok)
        self.assertGreaterEqual(wait, 49)
        self.assertTrue(self.u.allow(kid, t + 61)[0])            # ventana deslizante
        self.assertEqual(self.u.allow(None), (True, 0))
        for bad in (-1, users.MAX_RPM + 1, "x"):
            with self.assertRaises(users.UserError):
                self.u.set_key_limit(kid, bad)
        with self.assertRaises(users.UserError):
            self.u.create_key("admin", "mala", rpm=-5)
        self.assertEqual(len(self.u.data["keys"]), 1)             # la clave inválida no se queda a medias
        self.u.set_key_limit(kid, 0)
        self.assertTrue(all(self.u.allow(kid, t + 100)[0] for _ in range(50)))
        self.assertEqual(self.u.view()["keys"][kid]["rpm"], 0)

    def test_bloqueo_configurable(self):
        self.assertEqual(self.u.view()["security"], users.DEFAULT_SECURITY)
        self.u.set_security({"max_fails": 3, "window": 60, "lockout": 600, "user_lockout": True})
        for bad in ({"max_fails": 0}, {"window": 5}, {"lockout": "x"}, {"otro": 1}):
            with self.assertRaises(users.UserError):
                self.u.set_security(bad)
        t = 5000.0
        for i in range(3):
            self.assertEqual(self.u.login_blocked("ana", "10.0.0.9", now=t + i), 0)
            self.u.login_failed("ana", "10.0.0.9", now=t + i)
        self.assertGreater(self.u.login_blocked("ana", "10.0.0.9", now=t + 3), 590)
        # el usuario queda bloqueado desde otra IP de la red, pero no desde el propio equipo
        self.assertGreater(self.u.login_blocked("ana", "10.0.0.50", now=t + 3), 0)
        self.assertEqual(self.u.login_blocked("ana", "127.0.0.1", local=True, now=t + 3), 0)
        self.assertEqual(self.u.login_blocked("bob", "10.0.0.50", now=t + 3), 0)
        self.assertEqual(self.u.login_blocked("ana", "10.0.0.9", now=t + 603), 0)   # termina el bloqueo
        # fallos dispersos (fuera de la ventana) no bloquean
        for i in range(3):
            self.u.login_failed("eva", "10.0.0.7", now=t + 1000 + i * 100)
        self.assertEqual(self.u.login_blocked("eva", "10.0.0.7", now=t + 1201), 0)
        self.u.set_security({"user_lockout": False})
        self.u.login_ok("ana", "10.0.0.9")
        for i in range(3):
            self.u.login_failed("ana", "10.0.0.9", now=t + 2000 + i)
        self.assertEqual(self.u.login_blocked("ana", "10.0.0.50", now=t + 2003), 0)


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

    def test_4_auditoria(self):
        tok = Path(self.home, "local.token").read_text().strip()
        st, k, _ = self.req("POST", "/api/keys/create", {"user": "ana", "label": "aud"}, bearer=tok)
        self.assertEqual(self.req("GET", "/api/audit", bearer=k["key"])[0], 403)  # ana no tiene el permiso «audit»
        st, j, _ = self.req("GET", "/api/audit?limit=500", bearer=tok)
        self.assertEqual(st, 200, j)
        acts = [(e["action"], e["user"], e["status"]) for e in j["entries"]]
        self.assertIn(("POST /api/users/create", "admin", 200), acts)
        self.assertIn(("POST /api/unload", "ana", 403), acts)            # también lo rechazado
        self.assertIn(("GET /api/join-code", "ana", 403), acts)
        self.assertIn(("login", "nadie", 401), acts)
        self.assertIn(("login", "nadie", 429), acts)
        self.assertIn(("POST /api/me/password", "admin", 200), acts)
        self.assertNotIn("GET /api/state", [a[0] for a in acts])        # las consultas no se auditan
        raw = Path(self.home, "audit.jsonl").read_text(encoding="utf-8")
        for secret in ("contraseña-ana", "nueva-clave-admin", "\"admin\", \"new\""):
            self.assertNotIn(secret, raw)
        self.assertIn('"via": "session"', raw)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(Path(self.home, "audit.jsonl")).st_mode), 0o600)
        st, j, _ = self.req("GET", "/api/audit?user=ana&failed=1", bearer=tok)
        self.assertTrue(j["entries"] and all(e["user"] == "ana" and not e["ok"] for e in j["entries"]))

    def test_5_limite_de_clave(self):
        tok = Path(self.home, "local.token").read_text().strip()
        st, k, _ = self.req("POST", "/api/keys/create", {"user": "admin", "label": "lim", "rpm": 2}, bearer=tok)
        self.assertEqual(st, 200, k)
        codes = [self.req("GET", "/api/state", bearer=k["key"])[0] for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
        st, j, _ = self.req("POST", "/api/keys/limit", {"id": k["id"], "rpm": 0}, bearer=tok)
        self.assertEqual(st, 200, j)
        self.assertEqual(self.req("GET", "/api/state", bearer=k["key"])[0], 200)
        self.assertEqual(self.req("POST", "/api/keys/limit", {"id": k["id"], "rpm": -1}, bearer=tok)[0], 400)
        st, j, _ = self.req("POST", "/api/users/security", {"security": {"max_fails": 7}}, bearer=tok)
        self.assertEqual((st, j["security"]["max_fails"]), (200, 7))

    def test_6_webhooks(self):
        from test_webhooks import Receiver
        rx = Receiver()
        try:
            tok = Path(self.home, "local.token").read_text().strip()
            st, j, _ = self.req("POST", "/api/webhooks/set", {"name": "alertas", "url": rx.url + "/h",
                                                               "kinds": ["security"]}, bearer=tok)
            self.assertEqual(st, 200, j)
            self.assertTrue(j["secret"])
            st, r, _ = self.req("POST", "/api/webhooks/test", {"id": j["id"]}, bearer=tok)
            self.assertEqual((st, r["ok"]), (200, True))
            self.login("intruso", "mal")  # evento de seguridad (aviso) → webhook
            got = rx.wait(2)
            ev = json.loads(got[-1][2])["event"]
            self.assertEqual((ev["kind"], ev["level"]), ("security", "warn"))
            self.assertIn("intruso", ev["msg"])
            st, v, _ = self.req("GET", "/api/webhooks", bearer=tok)
            self.assertNotIn(j["secret"], json.dumps(v))
            self.assertNotIn(j["secret"], Path(self.home, "audit.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(self.req("POST", "/api/webhooks/set", {"url": "file:///etc/passwd"}, bearer=tok)[0], 400)
        finally:
            rx.close()


if __name__ == "__main__":
    unittest.main()
