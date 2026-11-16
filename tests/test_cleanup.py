# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Limpieza a fondo del desinstalador: identificar y cerrar procesos de DLLM y de la versión anterior, borrar restos."""
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipapp
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dllm import cleanup  # noqa: E402


def P(pid, ppid, name, cmd, exe=""):
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd, "exe": exe}


class Identify(unittest.TestCase):
    def test_python_and_engines(self):
        old = cleanup.LEGACY
        procs = [
            P(1, 0, "init", "init"),
            P(10, 1, "pythonw.exe", r'"C:\Python\pythonw.exe" "C:\Users\a\.dllm\dllm.pyz" agent --secret x'),
            P(11, 1, "python.exe", rf'python.exe C:\x\{old}.pyz up'),                # versión anterior
            P(12, 1, "python3", "python3 -m dllm agent --pairing"),
            P(13, 1, "python3", "python3 -m http.server"),                            # ajeno
            P(14, 1, "python3", "python3 /home/a/dllm/script.py"),                    # solo lleva el nombre en la ruta
            P(20, 10, "llama-server.exe", "llama-server.exe -m x.gguf", r"C:\Users\a\DLLM\llama.cpp\b1\llama-server.exe"),
            P(21, 10, "rpc-server", "rpc-server --port 1", "/opt/otro/rpc-server"),   # de otro programa
            P(22, 1, "ggml-rpc-server", "ggml-rpc-server", f"/home/a/{old.upper()}/llama.cpp/b1/ggml-rpc-server"),
        ]
        got = {p["pid"] for p in cleanup.find_ours(procs, me=999)}
        self.assertEqual(got, {10, 11, 12, 20, 22})
        # un directorio extra (carpeta de llama.cpp configurada por la persona) también cuenta
        self.assertIn(21, {p["pid"] for p in cleanup.find_ours(procs, ["/opt/otro"], me=999)})

    def test_never_kills_itself_or_launcher(self):
        procs = [P(5, 1, "py.exe", "py -3 dllm.pyz uninstall"), P(6, 5, "python.exe", "python.exe dllm.pyz uninstall"),
                 P(7, 1, "python.exe", "python.exe dllm.pyz agent")]
        self.assertEqual([p["pid"] for p in cleanup.find_ours(procs, me=6)], [7])


class KillAll(unittest.TestCase):
    def test_repeats_until_empty_and_reports_survivors(self):
        alive = {10: P(10, 1, "python3", "python3 -m dllm agent"), 20: P(20, 10, "rpc-server", "rpc-server", "/x/dllm/rpc-server")}
        killed = []

        def kill(pid):
            killed.append(pid)
            alive.pop(pid, None)
        closed, left = cleanup.kill_all(list_fn=lambda: list(alive.values()), kill_fn=kill)
        self.assertEqual(killed, [10, 20])  # primero el programa, luego el motor
        self.assertEqual(left, [])
        self.assertEqual(len(closed), 2)
        closed, left = cleanup.kill_all(list_fn=lambda: [P(30, 1, "python3", "python3 -m dllm up")], kill_fn=lambda pid: None, rounds=2)
        self.assertEqual(left, ["python3 (PID 30)"])

    def test_dry_run_kills_nothing(self):
        called = []
        closed, left = cleanup.kill_all(dry_run=True, list_fn=lambda: [P(7, 1, "python3", "python3 -m dllm up")],
                                        kill_fn=called.append)
        self.assertEqual((called, left), ([], []))
        self.assertEqual(len(closed), 1)

    @unittest.skipIf(os.name == "nt", "prueba con un proceso real en Linux/macOS")
    def test_kills_real_process(self):
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "app"
        src.mkdir()
        (src / "__main__.py").write_text("import time\ntime.sleep(120)\n")
        pyz = tmp / "dllm.pyz"
        zipapp.create_archive(src, pyz)
        proc = subprocess.Popen([sys.executable, str(pyz)])
        try:
            time.sleep(0.5)
            closed, left = cleanup.kill_all()
            self.assertTrue(any(str(proc.pid) in c for c in closed), closed)
            self.assertEqual(left, [])
            self.assertIsNotNone(proc.wait(timeout=10))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class RemoveDirs(unittest.TestCase):
    def test_force_rmtree_readonly(self):
        tmp = Path(tempfile.mkdtemp()) / "d"
        (tmp / "sub").mkdir(parents=True)
        f = tmp / "sub" / "a.txt"
        f.write_text("x")
        os.chmod(f, stat.S_IREAD)
        os.chmod(tmp / "sub", stat.S_IREAD | stat.S_IEXEC)
        self.assertTrue(cleanup.force_rmtree(tmp, attempts=2))
        self.assertFalse(tmp.exists())

    def test_legacy_dirs_never_include_models(self):
        conf, llama = cleanup.legacy_dirs()
        self.assertTrue(all("models" not in str(d) for d in conf + llama))


if __name__ == "__main__":
    unittest.main()
