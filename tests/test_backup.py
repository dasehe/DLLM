# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Copia de seguridad y restauración de la configuración: cifrado, integridad, fusión de config.json, CLI y API."""
import base64
import hashlib
import http.client
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from dllm import backup  # noqa: E402
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402

SECRET = "secreto-backup-1234567890abcdefgh"
CFG = {"version": 1, "role": "orchestrator", "name": "viejo", "secret": SECRET, "install_dir": "/viejo/DLLM",
       "models_dir": "/viejo/models", "llama_tag": "b1",
       "orchestrator": {"host": "127.0.0.1", "port": 8080, "api_key": "api-key-vieja", "min_mbps": 500, "beacon_port": 50999},
       "agent": {"port": 8765}}


def make_home(path, cfg=CFG):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps(cfg))
    (path / "models.json").write_text(json.dumps({"hf/a__b/m.gguf": {"temperature": 0.3}}))
    (path / "nodes.json").write_text(json.dumps({"n1": {"allow_slow": True}}))
    (path / "secrets.json").write_text(json.dumps({"hf_token": "hf_" + "x" * 30}))
    (path / "nodes_added.json").write_text(json.dumps([{"host": "10.0.0.9", "port": 8765}]))
    (path / "events.jsonl").write_text('{"msg":"x"}\n')
    return path


class TestCrypto(unittest.TestCase):
    def test_roundtrip_and_failures(self):
        data = os.urandom(5000)
        blob = backup.encrypt(data, "contraseña larga")
        self.assertNotIn(data[:32], blob)
        self.assertEqual(backup.decrypt(blob, "contraseña larga"), data)
        with self.assertRaises(backup.BackupError):
            backup.decrypt(blob, "otra contraseña")
        bad = bytearray(blob)
        bad[60] ^= 1
        with self.assertRaises(backup.BackupError):
            backup.decrypt(bytes(bad), "contraseña larga")
        with self.assertRaises(backup.BackupError):
            backup.encrypt(b"x", "corta")
        self.assertNotEqual(backup.encrypt(data, "contraseña larga"), blob)   # sal y nonce aleatorios


class TestBuildOpen(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = make_home(self.tmp / "home")

    def test_encrypted_roundtrip_and_summary(self):
        inv = [{"name": "hf/a__b/m.gguf", "size": 5, "source": "hf", "hf": {"repo": "a/b", "file": "m.gguf"}}]
        blob = backup.build(self.home, inv, "0.6.0", "equipo", False, "contraseña-segura")
        self.assertTrue(backup.is_encrypted(blob))
        self.assertNotIn(SECRET.encode(), blob)
        with self.assertRaises(backup.BackupError):
            backup.open_backup(blob)                      # falta la contraseña
        man, files = backup.open_backup(blob, "contraseña-segura")
        self.assertNotIn("events.jsonl", files)           # sin registros salvo que se pidan
        s = backup.summary(man, files)
        self.assertEqual((s["role"], s["model_settings"], s["node_settings"], s["nodes_added"], s["hf_token"]),
                         ("orchestrator", 1, 1, 1, True))
        self.assertEqual(s["models"], inv)
        _, files = backup.open_backup(backup.build(self.home, None, "x", "h", True, None))
        self.assertIn("events.jsonl", files)              # con registros y sin cifrar

    def test_tampering_and_forbidden_members(self):
        raw = backup.build(self.home, None, "x", "h", False, None)
        z = zipfile.ZipFile(io.BytesIO(raw))
        man = json.loads(z.read("manifest.json"))

        def rebuild(manifest, members):
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w") as o:
                o.writestr("manifest.json", json.dumps(manifest))
                for n, d in members.items():
                    o.writestr(n, d)
            return b.getvalue()
        members = {"files/" + n: z.read("files/" + n) for n in man["files"]}
        members["files/models.json"] = b'{"hf/x": {"temperature": 9}}'          # contenido cambiado
        with self.assertRaisesRegex(backup.BackupError, "suma de comprobación"):
            backup.open_backup(rebuild(man, members))
        evil = dict(man, files=dict(man["files"], **{"../../.bashrc": {"sha256": hashlib.sha256(b"x").hexdigest(), "size": 1}}))
        with self.assertRaisesRegex(backup.BackupError, "no permitido"):
            backup.open_backup(rebuild(evil, dict({"files/" + n: z.read("files/" + n) for n in man["files"]},
                                                  **{"files/../../.bashrc": b"x"})))
        with self.assertRaises(backup.BackupError):
            backup.open_backup(b"no es una copia")
        with self.assertRaises(backup.BackupError):
            backup.open_backup(rebuild(dict(man, app="otro"), {}))

    def test_restore_merges_config_and_keeps_safety_copy(self):
        blob = backup.build(self.home, None, "x", "h", False, None)
        _, files = backup.open_backup(blob)
        new = make_home(self.tmp / "nuevo", dict(CFG, role="both", name="nuevo", secret="otro-secreto",
                                                  install_dir="/nuevo/DLLM", models_dir="/nuevo/models", llama_tag="b9",
                                                  orchestrator={"api_key": "nueva"}))
        (new / "models.json").write_text("{}")
        actions, safety = backup.restore(files, new)
        cfg = json.loads((new / "config.json").read_text())
        self.assertEqual((cfg["secret"], cfg["orchestrator"]["api_key"], cfg["orchestrator"]["min_mbps"]),
                         (SECRET, "api-key-vieja", 500))                          # identidad del clúster restaurada
        self.assertEqual((cfg["role"], cfg["name"], cfg["install_dir"], cfg["models_dir"], cfg["llama_tag"]),
                         ("both", "nuevo", "/nuevo/DLLM", "/nuevo/models", "b9"))  # lo propio del equipo se conserva
        self.assertEqual(json.loads((new / "cluster.json").read_text())["secret"], SECRET)
        self.assertIn("hf/a__b/m.gguf", json.loads((new / "models.json").read_text()))
        self.assertTrue(safety and Path(safety).is_file())
        self.assertEqual(json.loads(backup.open_backup(Path(safety).read_bytes())[1]["config.json"])["secret"], "otro-secreto")
        if os.name != "nt":
            self.assertEqual(os.stat(new / "config.json").st_mode & 0o777, 0o600)
        self.assertTrue(any("config.json" in a for a in actions))


class TestCli(unittest.TestCase):
    def run_cli(self, home, *args, env=None):
        e = dict(os.environ, PYTHONPATH=ROOT, DLLM_HOME=str(home), **(env or {}))
        return subprocess.run([sys.executable, "-m", "dllm", "backup"] + list(args), capture_output=True, text=True,
                              env=e, cwd=ROOT, timeout=120)

    def test_export_import_inspect(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        home = make_home(tmp / "a")
        out = tmp / "c.dllmbak"
        r = self.run_cli(home, "export", str(out), env={"DLLM_BACKUP_PASSPHRASE": "contraseña-cli-1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(backup.is_encrypted(out.read_bytes()))
        r = self.run_cli(tmp / "b", "inspect", str(out), env={"DLLM_BACKUP_PASSPHRASE": "mala-contraseña"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("contraseña", r.stderr)
        r = self.run_cli(tmp / "b", "inspect", str(out), env={"DLLM_BACKUP_PASSPHRASE": "contraseña-cli-1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 nodo(s) añadidos por IP", r.stdout)
        r = self.run_cli(tmp / "b", "import", str(out), "--yes", env={"DLLM_BACKUP_PASSPHRASE": "contraseña-cli-1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((tmp / "b" / "config.json").read_text())["secret"], SECRET)
        plain = tmp / "p.dllmbak"
        r = self.run_cli(home, "export", str(plain), "--plain")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SIN CIFRAR", r.stdout)
        self.assertFalse(backup.is_encrypted(plain.read_bytes()))


@unittest.skipIf(os.name == "nt", "binarios simulados con shebang y grupos de procesos POSIX")
class BackupApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.models = os.path.join(cls.tmp, "models")
        os.makedirs(cls.models)
        os.makedirs(os.path.join(cls.models, "hf", "o__r"))
        write_gguf(os.path.join(cls.models, "hf", "o__r", "m.gguf"), 1 << 30)
        cls.procs, cls.op, cls.bp = [], free_port(), free_port()
        cls.hidden = free_port()
        mb = os.path.join(HERE, "mockbin")
        base = dict(os.environ, PYTHONPATH=ROOT)
        cls.ohome = cls.tmp + "/o"
        make_home(cls.ohome, dict(CFG, orchestrator=dict(CFG["orchestrator"], api_key=None)))  # un orquestador instalado tiene su configuración (config.json, models.json…)
        cls.spawn("v", dict(base, DLLM_HOME=cls.tmp + "/v"),
                  ["agent", "--secret", SECRET, "--name", "vis", "--port", str(free_port()), "--rpc-port", str(free_port()),
                   "--bin-dir", mb, "--backend", "cpu", "--mem-gb", "6", "--announce-to", f"127.0.0.1:{cls.bp}",
                   "--beacon-interval", "0.5"])
        cls.spawn("h", dict(base, DLLM_HOME=cls.tmp + "/h"),
                  ["agent", "--secret", SECRET, "--name", "oculto", "--port", str(cls.hidden), "--rpc-port", str(free_port()),
                   "--bin-dir", mb, "--backend", "cpu", "--mem-gb", "6", "--announce-to", "127.0.0.1:9",
                   "--beacon-interval", "5"])
        cls.spawn("o", dict(base, DLLM_HOME=cls.ohome),
                  ["up", "--secret", SECRET, "--models-dir", cls.models, "--port", str(cls.op), "--bin-dir", mb,
                   "--beacon-port", str(cls.bp), "--node-timeout", "4", "--probe-interval", "0.5"])
        try:
            cls.wait(lambda: any(n["up"] for n in cls.api("GET", "/api/state")[1]["nodes"]), 40, "nodo")
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
    def raw(cls, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.op, timeout=60)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        d = r.read()
        c.close()
        return r.status, d, r

    @classmethod
    def api(cls, method, path, body=None):
        st, d, _ = cls.raw(method, path, body)
        return st, json.loads(d or b"{}")

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
        raise AssertionError(f"timeout esperando: {what}")

    def test_export_preview_import_roundtrip(self):
        self.assertEqual(self.api("POST", "/api/nodes/add", {"host": "127.0.0.1", "port": self.hidden})[0], 200)
        self.assertIn(self.hidden, [x["port"] for x in json.loads((Path(self.ohome) / "nodes_added.json").read_text())])
        self.assertEqual(self.api("POST", "/api/models/settings", {"model": "hf/o__r/m.gguf",
                                                                   "settings": {"temperature": 0.2}})[0], 200)
        # sin contraseña ni «plain» explícito: se rechaza
        self.assertEqual(self.api("POST", "/api/backup/export", {})[0], 400)
        st, blob, resp = self.raw("POST", "/api/backup/export", {"passphrase": "contraseña-api-1"})
        self.assertEqual(st, 200)
        self.assertIn("attachment", resp.getheader("Content-Disposition"))
        self.assertTrue(backup.is_encrypted(blob))
        b64 = base64.b64encode(blob).decode()
        # estropeamos el estado: otro parámetro, y borramos el nodo añadido
        self.api("POST", "/api/models/settings", {"model": "hf/o__r/m.gguf", "settings": {"temperature": 1.5}})
        nid = next(n["id"] for n in self.api("GET", "/api/state")[1]["nodes"] if n["name"] == "oculto")
        self.assertEqual(self.api("POST", "/api/nodes/forget", {"node": nid})[0], 200)
        self.assertNotIn(self.hidden, [x["port"] for x in json.loads((Path(self.ohome) / "nodes_added.json").read_text())])
        # vista previa
        self.assertEqual(self.api("POST", "/api/backup/preview", {"data": b64, "passphrase": "mala-mala-1"})[0], 400)
        st, s = self.api("POST", "/api/backup/preview", {"data": b64, "passphrase": "contraseña-api-1"})
        self.assertEqual(st, 200, s)
        self.assertEqual((s["nodes_added"], s["secret_changes"], s["missing_models"]), (2, False, []))
        # importar exige confirmación
        self.assertEqual(self.api("POST", "/api/backup/import", {"data": b64, "passphrase": "contraseña-api-1"})[0], 400)
        st, r = self.api("POST", "/api/backup/import", {"data": b64, "passphrase": "contraseña-api-1", "confirm": True})
        self.assertEqual(st, 200, r)
        self.assertFalse(r["restart_required"])               # mismo secreto: no hace falta reiniciar
        self.assertTrue(r["safety_backup"])
        st, ms = self.api("GET", "/api/models")
        item = next(m for m in (ms if isinstance(ms, list) else ms.get("models", ms)) if m["name"] == "hf/o__r/m.gguf")
        self.assertEqual(item["settings"]["temperature"], 0.2)   # parámetro restaurado
        self.wait(lambda: any(n["name"] == "oculto" and n["up"] for n in self.api("GET", "/api/state")[1]["nodes"]), 30,
                  "nodo añadido restaurado")


if __name__ == "__main__":
    unittest.main(verbosity=2)
