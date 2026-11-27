# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Registro de auditoría: sin secretos, rotación, permisos del fichero y consultas con filtros."""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dllm import audit, orchestrator  # noqa: E402


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "audit.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sin_secretos(self):
        p = audit.sanitize({"name": "ana", "password": "x1", "passphrase": "x2", "data": "QUJD" * 100,
                            "hf_token": "hf_x", "key": "dllm_x", "old": "a", "new": "b", "empty_key": "",
                            "nested": {"api_key": "k", "n": 3}, "long": "z" * 500, "list": list(range(30))})
        txt = json.dumps(p)
        for s in ("x1", "x2", "QUJD", "hf_x", "dllm_x", '"a"', '"b"', '"k"'):
            self.assertNotIn(s, txt)
        self.assertEqual(p["name"], "ana")
        self.assertEqual(p["nested"]["n"], 3)
        self.assertEqual(p["empty_key"], "")
        self.assertLess(len(p["long"]), 210)
        self.assertEqual(p["list"][-1], "…")

    def test_fichero_rotacion_y_consulta(self):
        a = audit.Audit(self.path, max_bytes=2000, keep=2)
        for i in range(60):
            a.record(f"POST /api/x{i}", "ana" if i % 2 else "admin", "session", "10.0.0.5",
                     403 if i % 10 == 0 else 200, {"i": i})
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertFalse(os.path.exists(self.path + ".3"))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        q = a.query(limit=5)
        self.assertEqual([e["action"] for e in q], [f"POST /api/x{i}" for i in range(59, 54, -1)])
        self.assertTrue(all(e["user"] == "ana" for e in a.query(user="ana")))
        self.assertTrue(all(not e["ok"] for e in a.query(failed=True)))
        self.assertEqual(a.query(text="x57")[0]["params"], {"i": 57})
        self.assertEqual(a.query(since=a.query(limit=1)[0]["t"] + 1), [])

    def test_en_memoria(self):
        a = audit.Audit()
        a.record("login", "admin", "password", "127.0.0.1", 200)
        self.assertEqual(a.query()[0]["ok"], True)

    def test_que_se_audita(self):
        self.assertTrue(orchestrator.audited("POST", "/api/load"))
        self.assertTrue(orchestrator.audited("GET", "/api/join-code"))
        self.assertFalse(orchestrator.audited("GET", "/api/state"))
        self.assertFalse(orchestrator.audited("POST", "/api/plan"))
        self.assertFalse(orchestrator.audited("POST", "/v1/chat/completions"))
        self.assertEqual(orchestrator.perm_for("GET", "/api/audit"), "audit")


if __name__ == "__main__":
    unittest.main()
