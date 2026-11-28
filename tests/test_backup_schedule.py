# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Copias programadas: siempre cifradas, permisos 600, retención (solo borra las suyas), validación y frase oculta."""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dllm import backup, backup_schedule  # noqa: E402


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / "config.json").write_text('{"secret": "s"}')
        self.events = []
        self.s = backup_schedule.BackupScheduler(
            self.home, lambda pw, logs: backup.build(self.home, [], "t", "h", logs, pw),
            event=lambda m, lv: self.events.append((lv, m)), host="orq")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_validacion(self):
        for bad in ({"enabled": True}, {"every_hours": 0}, {"keep": 101}, {"dir": "relativa/x"},
                    {"passphrase": "corta"}, {"otro": 1}, [1]):
            with self.assertRaises(backup_schedule.ScheduleError, msg=bad):
                self.s.configure(bad)
        with self.assertRaises(backup_schedule.ScheduleError):
            self.s.run_now()  # sin frase no hay copia
        self.assertFalse(self.s.due())

    def test_copias_cifradas_y_retencion(self):
        out = self.tmp / "nas"
        out.mkdir()
        (out / "mio.txt").write_text("no tocar")
        (out / "dllm-manual.dllmbak").write_bytes(b"x")  # copia manual del usuario: no se borra
        v = self.s.configure({"enabled": True, "every_hours": 6, "keep": 2, "dir": str(out),
                              "passphrase": "frase-secreta-1"})
        self.assertNotIn("frase-secreta-1", json.dumps(v))
        self.assertTrue(v["has_passphrase"])
        self.assertTrue(self.s.due())                          # nunca hecha: toca ya
        names = []
        for _ in range(3):
            r = self.s.run_now()
            self.assertTrue(r["ok"], r)
            names.append(Path(r["file"]).name)
            time.sleep(1.05)                                   # nombres con segundos distintos
        left = sorted(p.name for p in out.iterdir())
        self.assertEqual(left, sorted(["mio.txt", "dllm-manual.dllmbak"] + names[1:]))
        f = out / names[-1]
        blob = f.read_bytes()
        self.assertTrue(backup.is_encrypted(blob))
        man, files = backup.open_backup(blob, "frase-secreta-1")
        self.assertIn("config.json", files)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((self.home / "backup_schedule.json").stat().st_mode), 0o600)
        self.assertFalse(self.s.due())                         # recién hecha
        self.assertTrue(self.s.due(now=time.time() + 6 * 3600 + 1))
        # vacío conserva la frase; la vista muestra última copia y ficheros
        v = self.s.configure({"passphrase": "", "keep": 5})
        self.assertTrue(v["has_passphrase"] and v["last"]["ok"])
        self.assertEqual(len(v["files"]), 2)
        self.assertTrue(any(lv == "info" and "cifrada" in m for lv, m in self.events))
        # persistencia
        s2 = backup_schedule.BackupScheduler(self.home, lambda *a: b"", host="orq")
        self.assertEqual((s2.view()["keep"], s2.view()["enabled"]), (5, True))

    def test_fallo_registrado(self):
        def boom(pw, logs):
            raise OSError("disco lleno")
        s = backup_schedule.BackupScheduler(self.home, boom, event=lambda m, lv: self.events.append((lv, m)))
        s.configure({"passphrase": "frase-secreta-1", "dir": str(self.tmp / "x")})
        r = s.run_now()
        self.assertFalse(r["ok"])
        self.assertIn("disco lleno", s.view()["last"]["error"])
        self.assertEqual(self.events[-1][0], "error")


if __name__ == "__main__":
    unittest.main()
