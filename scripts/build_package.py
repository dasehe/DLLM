#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Construye el paquete distribuible: un único dllm.pyz + lanzadores para Windows/macOS/Linux.

Uso: python scripts/build_package.py [--out DIR]
Salida: <out>/dllm-package-<version>/  y  <out>/dllm-package-<version>.zip  (+ SHA256SUMS)
"""
import argparse
import hashlib
import shutil
import stat
import sys
import tempfile
import zipapp
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dllm import __version__  # noqa: E402

LEGAL = ("LICENSE", "NOTICE", "AUTHORS.md")
LAUNCHERS = {"install.sh": 0o755, "Install-DLLM.command": 0o755, "Install-DLLM.bat": 0o644, "LEEME-INSTALAR.md": 0o644, "README-INSTALL.md": 0o644,
             "uninstall.sh": 0o755, "Uninstall-DLLM.command": 0o755, "Uninstall-DLLM.bat": 0o644,
             "Open-DLLM.bat": 0o644}


def build(out_root):
    name = f"dllm-package-{__version__}"
    out = Path(out_root) / name
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "src"
        shutil.copytree(ROOT / "dllm", stage / "dllm", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in LEGAL:  # los avisos legales viajan también dentro del .pyz
            if (ROOT / f).exists():
                shutil.copy2(ROOT / f, stage / f)
        zipapp.create_archive(stage, out / "dllm.pyz", interpreter="/usr/bin/env python3",
                              main="dllm.cli:main", compressed=True)
    (out / "dllm.pyz").chmod(0o755)
    for f, mode in LAUNCHERS.items():
        data = (ROOT / "packaging" / f).read_bytes()
        if f.endswith(".bat"):
            data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")  # CRLF para cmd.exe
        (out / f).write_bytes(data)
        (out / f).chmod(mode)
    shutil.copy2(ROOT / "scripts" / "build-llamacpp.sh", out / "build-llamacpp.sh")
    for f in LEGAL:
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, out / f)
        else:
            print(f"AVISO: falta {f} en la raíz del repositorio; el paquete se genera sin él", file=sys.stderr)
    sums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                   for p in sorted(out.iterdir()) if p.is_file())
    (out / "SHA256SUMS").write_text(sums)
    zpath = Path(out_root) / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.iterdir()):
            zi = zipfile.ZipInfo(f"{name}/{p.name}", date_time=(2026, 1, 1, 0, 0, 0))
            zi.external_attr = (p.stat().st_mode & 0o777 | stat.S_IFREG) << 16  # conserva el bit ejecutable
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, p.read_bytes())
    return out, zpath


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args()
    out, z = build(a.out)
    print(f"paquete: {out}\nzip:     {z} ({z.stat().st_size // 1024} KiB)")
