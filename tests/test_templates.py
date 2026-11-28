# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Plantillas de despliegue: validación, persistencia y destinos."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dllm import orchestrator, templates  # noqa: E402


class TemplatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.t = templates.Templates(self.tmp / "templates.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_guardar_aplicar_borrar(self):
        tg = [{"node": "vAI1", "device": "both", "ram_gb": 6, "priority": 5}, {"node": "vAI2", "device": "cpu"}]
        name, t = self.t.save({"name": "gemma grande", "model": "ollama/gemma4-31b.gguf", "ctx": 8192, "slot": 0,
                               "targets": tg}, orchestrator.Cluster.parse_targets)
        self.assertEqual((t["slot"], t["pinned"], t["targets"]), (0, True, tg))
        self.assertEqual(templates.Templates(self.t.path).get("gemma grande")["ctx"], 8192)  # persistencia
        self.t.save({"name": "gemma grande", "model": "otro.gguf"})                            # sustituye
        self.assertEqual(self.t.get("gemma grande")["model"], "otro.gguf")
        self.t.delete("gemma grande")
        self.assertEqual(self.t.view()["templates"], {})
        with self.assertRaises(templates.TemplateError):
            self.t.get("gemma grande")

    def test_validacion(self):
        pt = orchestrator.Cluster.parse_targets
        for bad in ({"name": "", "model": "m"}, {"name": "../x", "model": "m"}, {"name": "a"},
                    {"name": "a", "model": "m", "ctx": 10}, {"name": "a", "model": "m", "slot": 3},
                    {"name": "a", "model": "m", "targets": [{"node": "n", "device": "tpu"}]},
                    {"name": "a", "model": "m", "targets": [{"node": "n", "priority": 99}]}, []):
            with self.assertRaises((templates.TemplateError, ValueError), msg=bad):
                self.t.save(bad, pt)
        with self.assertRaises(templates.TemplateError):
            self.t.delete("no-existe")


if __name__ == "__main__":
    unittest.main()
