# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Un agente no debe anunciarse si su puerto HTTP ya lo usa otro proceso (p. ej. un agente de una instalación anterior)."""
import os
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ports import free_port  # noqa: E402


class AgentPortBusy(unittest.TestCase):
    def test_exits_with_clear_message_when_port_taken(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        home = tempfile.mkdtemp()
        env = dict(os.environ, DLLM_HOME=home, PYTHONPATH=ROOT)
        try:
            r = subprocess.run([sys.executable, "-m", "dllm", "agent", "--pairing", "--name", "x", "--port", str(port),
                                "--rpc-port", str(free_port())],
                               capture_output=True, text=True, timeout=60, env=env, cwd=ROOT)
        finally:
            s.close()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("otro proceso", r.stderr)
        self.assertIn(str(port), r.stderr)


class ExitRecord(unittest.TestCase):
    def test_keeps_relevant_lines(self):
        from dllm import agent
        tail = ["ggml_vulkan: Using AMD", "ggml_vulkan: Device memory allocation failed", "otra cosa"] + ["x"] * 3
        r = agent.exit_record(0, -1073740791, tail, "porque sí")
        self.assertEqual((r["slot"], r["code"], r["why"]), (0, -1073740791, "porque sí"))
        self.assertIn("ggml_vulkan: Device memory allocation failed", r["lines"])
        self.assertEqual(agent.exit_record(0, 1, [])["lines"], [])


if __name__ == "__main__":
    unittest.main()
