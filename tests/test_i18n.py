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

    def test_frases_con_valores_completas(self):
        falta = i18n_extract.templates_missing()
        self.assertEqual(falta, [], "frases con valores que quedarían a medio traducir (añádelas a "
                                    "dllm/i18n/en.tpl.json; scripts/i18n_extract.py --templates las lista):\n"
                                    + "\n".join(falta[:40]))
        for src, dst in i18n.templates("en").items():  # mismos marcadores en origen y traducción
            self.assertEqual(sorted(re.findall(r"\{\d+\}", src)), sorted(re.findall(r"\{\d+\}", dst)), src)

    def test_plantillas_python(self):
        self.assertEqual(i18n.tr("nodo vAI2 de nuevo en línea", "en"), "node vAI2 back online")
        self.assertEqual(i18n.tr("descarga en cola: ollama gemma4:31b + proyector multimodal", "en"),
                         "download queued: ollama gemma4:31b + multimodal projector")
        self.assertEqual(i18n.tr("0 errores · 2 avisos · 5 informativos", "en"), "0 errors · 2 warnings · 5 informational")
        self.assertEqual(i18n.tr("vAI2: emparejamiento rechazada", "en"), "vAI2: pairing rejected")  # valor traducido
        self.assertEqual(i18n.tr("nodo vAI2 de nuevo en línea", "es"), "nodo vAI2 de nuevo en línea")

    @unittest.skipUnless(shutil.which("node"), "sin node")
    def test_plantillas_runtime_js(self):
        """El traductor del navegador aplica las mismas plantillas (se ejecuta con node y un DOM mínimo)."""
        out = i18n.inject("<!--I18N-->")
        js = re.findall(r"<script>(.*?)</script>", out, re.S)
        stub = ("var location={search:''};var localStorage={getItem:function(){return 'en'},setItem:function(){}};"
                "var document={readyState:'loading',documentElement:{},addEventListener:function(){},"
                "getElementById:function(){return null}};var window=globalThis;"
                "window.alert=window.confirm=window.prompt=function(){};")
        test = ("console.log(JSON.stringify([L('nodo vAI2 de nuevo en línea'),"
                "L('reclamando vAI2 (192.168.1.153): código de verificación 398 499'),"
                "L('0 errores · 2 avisos · 5 informativos'),L('todos')]))")
        r = subprocess.run(["node"], input=stub + "\n".join(js) + "\n" + test, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), ["node vAI2 back online",
                                                "claiming vAI2 (192.168.1.153): verification code 398 499",
                                                "0 errors · 2 warnings · 5 informational", "all"])

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
