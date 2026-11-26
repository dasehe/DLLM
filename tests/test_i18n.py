# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Idiomas: toda cadena visible de las páginas tiene traducción al inglés (idioma por defecto) y el traductor se inyecta."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import i18n_extract  # noqa: E402
from dllm import i18n  # noqa: E402


class I18n(unittest.TestCase):
    def test_todo_traducido(self):
        falta = i18n_extract.missing()
        self.assertEqual(falta, [], "textos sin traducir (añádelos a dllm/i18n/en.json; "
                                    "scripts/i18n_extract.py --json los lista):\n" + "\n".join(falta[:40]))

    def test_traducciones_no_vacias_y_patrones_validos(self):
        d = i18n.dictionary("en")
        self.assertTrue(all(isinstance(v, str) and v.strip() for v in d.values()))
        for pat, rep in i18n.patterns("en"):
            re.compile(pat.replace("$1", r"\1"))
            self.assertIsInstance(rep, str)

    def test_inyeccion(self):
        for page in ("ui.html", "setup.html", "uninstall.html"):
            with open(os.path.join(ROOT, "dllm", page), encoding="utf-8") as f:
                html = f.read()
            self.assertIn("<!--I18N-->", html, page)
            self.assertIn('id="langsel"', html, page)
            out = i18n.inject(html)
            self.assertNotIn("<!--I18N-->", out)
            self.assertIn("window.__I18N=", out)
            self.assertIn("window.setLang", out)
            data = out.split("window.__I18N=", 1)[1].split(";window.__I18N_RE=", 1)[0]
            self.assertEqual(json.loads(data), i18n.dictionary("en"))

    @unittest.skipUnless(shutil.which("node"), "sin node")
    def test_runtime_sintaxis(self):
        r = subprocess.run(["node", "--check", os.path.join(ROOT, "dllm", "i18n", "runtime.js")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()


class TerminalYApi(unittest.TestCase):
    def test_tr_python(self):
        self.assertEqual(i18n.tr("ya hay una carga en curso", "en"), "a load is already in progress")
        self.assertEqual(i18n.tr("ya hay una carga en curso", "es"), "ya hay una carga en curso")
        self.assertEqual(i18n.tr("  Nodos  ", "en"), "  Nodes  ")
        self.assertIn("6.0 GiB free", i18n.tr("6.0 GiB libres", "en"))
        e = i18n.tr_error({"error": {"message": "petición demasiado grande", "type": "x"}}, "en")
        self.assertEqual(e["error"]["message"], "request too large")
        self.assertEqual(i18n.tr_error({"error": "nodo desconocido"}, "es"), {"error": "nodo desconocido"})

    def test_cli_en_ingles_por_defecto(self):
        import tempfile
        env = dict(os.environ, DLLM_HOME=tempfile.mkdtemp())
        env.pop("DLLM_LANG", None)
        r = subprocess.run([sys.executable, "-m", "dllm", "join-code"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertIn("this computer is not an orchestrator", r.stdout + r.stderr)
        r = subprocess.run([sys.executable, "-m", "dllm", "--lang", "es", "join-code"], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        self.assertIn("este equipo no es un orquestador", r.stdout + r.stderr)

    def test_lang_de_la_peticion(self):
        self.assertEqual(i18n.request_lang({"X-DLLM-Lang": "es"}), "es")
        self.assertEqual(i18n.request_lang({}, "a=1&lang=en"), "en")
