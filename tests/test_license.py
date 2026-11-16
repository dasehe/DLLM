# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Avisos de licencia y atribución: deben estar en el código, en las interfaces y en el paquete."""
import os
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dllm  # noqa: E402

AUTHOR = "David Serrano Hernando"


class TestLicense(unittest.TestCase):
    def test_notice_has_attribution_terms(self):
        n = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertIn(AUTHOR, n)
        self.assertIn("AGPL-3.0-or-later", n)
        self.assertIn("7.b", n)
        self.assertIn(dllm.ATTRIBUTION.split(" — ")[1], n)

    def test_every_source_file_has_spdx_header(self):
        files = list((ROOT / "dllm").glob("*.py")) + list((ROOT / "scripts").glob("*.py")) + \
            list((ROOT / "tests").glob("*.py")) + list((ROOT / "tests" / "mockbin").iterdir())
        missing = [str(f.relative_to(ROOT)) for f in files
                   if "SPDX-License-Identifier: AGPL-3.0-or-later" not in f.read_text(encoding="utf-8")[:400]]
        self.assertEqual(missing, [], "añade la cabecera SPDX (ver CONTRIBUTING.md)")

    def test_interfaces_show_attribution_and_source(self):
        self.assertIn(AUTHOR, dllm.legal_notice())
        self.assertIn(dllm.SOURCE_URL, dllm.legal_notice())
        for page in ("ui.html", "setup.html"):
            html = (ROOT / "dllm" / page).read_text(encoding="utf-8")
            self.assertIn(AUTHOR, html, page)
            self.assertIn("AGPL-3.0-or-later", html, page)
            self.assertIn("github.com/dasehe/dllm", html, page)  # sección 13: oferta del código fuente

    def test_package_ships_legal_files(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import tempfile
        import build_package
        with tempfile.TemporaryDirectory() as d:
            out, z = build_package.build(d)
            self.assertTrue((out / "NOTICE").exists())
            with zipfile.ZipFile(out / "dllm.pyz") as pyz:
                self.assertIn("NOTICE", pyz.namelist())
            if (ROOT / "LICENSE").exists():
                self.assertTrue((out / "LICENSE").exists())
                self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", (ROOT / "LICENSE").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
