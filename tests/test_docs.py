# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Documentación bilingüe: cada documento existe en inglés (docs/) y en español (docs/es/), se enlazan entre sí y los
enlaces relativos apuntan a ficheros que existen."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAIRS = {"ARCHITECTURE": "ARQUITECTURA", "DEVELOPMENT": "DESARROLLO", "INSTALLATION": "INSTALACION",
         "INTEGRATIONS": "INTEGRACIONES", "ROADMAP": "ROADMAP", "SECURITY": "SEGURIDAD",
         "TROUBLESHOOTING": "SOLUCION-PROBLEMAS", "USAGE": "USO"}


def links(path):
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    for m in re.finditer(r"\]\(([^)#\s]+)(#[^)]*)?\)", txt):
        u = m.group(1)
        if not re.match(r"^(https?:|mailto:)", u):
            yield u


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Docs(unittest.TestCase):
    def test_pares_y_enlaces_de_idioma(self):
        for en, es in PAIRS.items():
            pe, ps = os.path.join(ROOT, "docs", en + ".md"), os.path.join(ROOT, "docs", "es", es + ".md")
            self.assertTrue(os.path.exists(pe), pe)
            self.assertTrue(os.path.exists(ps), ps)
            self.assertIn(f"](es/{es}.md)", read(pe))
            self.assertIn(f"](../{en}.md)", read(ps))
        self.assertIn("](README.es.md)", read(os.path.join(ROOT, "README.md")))
        self.assertIn("](README.md)", read(os.path.join(ROOT, "README.es.md")))

    def test_enlaces_relativos_existen(self):
        files = ["README.md", "README.es.md", "CONTRIBUTING.md", "packaging/README-INSTALL.md",
                 "packaging/LEEME-INSTALAR.md"]
        files += ["docs/" + f for f in os.listdir(os.path.join(ROOT, "docs")) if f.endswith(".md")]
        files += ["docs/es/" + f for f in os.listdir(os.path.join(ROOT, "docs", "es")) if f.endswith(".md")]
        rotos = []
        for f in files:
            base = os.path.dirname(os.path.join(ROOT, f))
            for u in links(os.path.join(ROOT, f)):
                if not os.path.exists(os.path.normpath(os.path.join(base, u))):
                    rotos.append(f"{f} → {u}")
        self.assertEqual(rotos, [])


if __name__ == "__main__":
    unittest.main()
