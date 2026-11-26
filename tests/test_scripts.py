# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Instaladores de una sola orden: sintaxis y contrato mínimo (la ejecución completa necesita red y se prueba a mano)."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


class OneLiners(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "sin bash")
    def test_sh_sintaxis(self):
        for f in ("install.sh", "instalar-ubuntu.sh"):
            r = subprocess.run(["bash", "-n", str(SCRIPTS / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f + r.stderr)

    def test_contrato(self):
        sh = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
        ps = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
        for txt in (sh, ps):
            self.assertIn("SPDX-License-Identifier: AGPL-3.0-or-later", txt)
            self.assertIn("codeload.github.com", txt)          # descarga sin git
            self.assertIn("build_package.py", txt)
            self.assertIn("DLLM_LANG", txt)
            self.assertNotIn("http://", txt)                    # solo HTTPS
        self.assertIn("Authorization: token", sh)              # token en cabecera, nunca en la URL
        self.assertNotIn("@github.com", sh + ps)
        self.assertIn("winget install", ps)
        self.assertTrue(os.access(SCRIPTS / "install.sh", os.X_OK) or os.name == "nt")

    @unittest.skipUnless(shutil.which("pwsh"), "sin PowerShell")
    def test_ps1_sintaxis(self):
        cmd = ("$e=$null;[System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e)|Out-Null;"
               "if($e){$e;exit 1}" % (SCRIPTS / "install.ps1"))
        r = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
