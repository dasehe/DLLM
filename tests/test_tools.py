# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Herramientas (function calling) preparadas por DLLM: --jinja automático según la plantilla del modelo, capacidades en
/v1/models y prueba de herramientas. llama-server simulado (devuelve tool_calls solo si se lanzó con --jinja)."""
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _proc  # noqa: E402
from dllm import engine, gguf  # noqa: E402
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402

GiB = 1 << 30
SECRET = "secreto-herramientas-1234567890ab"
TPL_TOOLS = "{% if tools %}{{ tools | tojson }}{% endif %}{% for m in messages %}{{ m.content }}{% endfor %}"
TPL_PLAIN = "{% for m in messages %}{{ m.content }}{% endfor %}"


class TestCommand(unittest.TestCase):
    def cmd(self, **spec):
        return engine.driver_for(spec.pop("settings", {})).build_command(dict(
            {"bin": "llama-server", "model_path": "m.gguf", "port": 1, "ctx": 2048}, **spec))

    def test_cache_reuse_and_speculative_decoding(self):
        c = self.cmd(draft="/m/small.gguf", settings={"cache_reuse": 256, "draft_max": 16, "draft_min": 2})
        self.assertEqual(c[c.index("--cache-reuse") + 1], "256")
        self.assertEqual((c[c.index("-md") + 1], c[c.index("-ngld") + 1]), ("/m/small.gguf", "0"))
        self.assertEqual(c[c.index("--draft-max") + 1], "16")
        self.assertNotIn("-md", self.cmd(draft="/m/s.gguf", settings={"embeddings": True}))
        from dllm import models
        self.assertEqual(models.validate_settings({"draft_model": "hf/a/small.gguf", "draft_max": 8}),
                         {"draft_model": "hf/a/small.gguf", "draft_max": 8})
        for bad in ({"draft_model": "../x.gguf"}, {"draft_model": "/etc/x.gguf"}, {"draft_model": "x.bin"},
                    {"draft_min": 9, "draft_max": 4}, {"cache_reuse": -1}):
            with self.assertRaises(models.ModelError):
                models.validate_settings(bad)

    def test_jinja_only_when_asked_and_never_for_embeddings(self):
        self.assertNotIn("--jinja", self.cmd())
        self.assertIn("--jinja", self.cmd(jinja=True))
        self.assertNotIn("--jinja", self.cmd(jinja=True, settings={"embeddings": True}))


class TestGgufTemplate(unittest.TestCase):
    def test_detects_tool_support(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name, tpl in (("a.gguf", TPL_TOOLS), ("b.gguf", TPL_PLAIN), ("c.gguf", None)):
            write_gguf(os.path.join(d, name), 1 << 20, chat_template=tpl)
        r = {n: gguf.read_model(os.path.join(d, n)) for n in ("a.gguf", "b.gguf", "c.gguf")}
        self.assertEqual([(r[n]["chat_template"], r[n]["tool_template"]) for n in r],
                         [(True, True), (True, False), (False, False)])


@unittest.skipIf(os.name == "nt", "binarios simulados con shebang y grupos de procesos POSIX")
class ToolsE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        models = os.path.join(cls.tmp, "models")
        os.makedirs(models)
        write_gguf(os.path.join(models, "tools.gguf"), 1 * GiB, chat_template=TPL_TOOLS)
        write_gguf(os.path.join(models, "plain.gguf"), 1 * GiB, chat_template=TPL_PLAIN)
        write_gguf(os.path.join(models, "notpl.gguf"), 1 * GiB)
        cls.procs, cls.op, cls.bp = [], free_port(), free_port()
        mb = os.path.join(HERE, "mockbin")
        base = dict(os.environ, PYTHONPATH=ROOT)
        for name, extra in (("a", []), ("b", [])):
            cls.spawn(name, dict(base, DLLM_HOME=f"{cls.tmp}/{name}"),
                      ["agent", "--secret", SECRET, "--name", name, "--port", str(free_port()), "--rpc-port",
                       str(free_port()), "--bin-dir", mb, "--backend", "cpu", "--mem-gb", "8", "--bw-gbps", "30",
                       "--announce-to", f"127.0.0.1:{cls.bp}", "--beacon-interval", "0.5"])
        cls.spawn("orch", dict(base, DLLM_HOME=f"{cls.tmp}/o"),
                  ["up", "--secret", SECRET, "--models-dir", models, "--port", str(cls.op), "--bin-dir", mb,
                   "--beacon-port", str(cls.bp), "--node-timeout", "4", "--probe-interval", "0.5"])
        try:
            cls.wait(lambda: len([n for n in cls.api("GET", "/api/state")[1]["nodes"] if n["up"]]) == 2, 40, "nodos")
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def spawn(cls, name, env, args):
        log = open(os.path.join(cls.tmp, name + ".log"), "w")
        cls.procs.append(subprocess.Popen([sys.executable, "-m", "dllm"] + args, cwd=ROOT, env=env, stdout=log,
                                          stderr=subprocess.STDOUT, start_new_session=True))

    @classmethod
    def tearDownClass(cls):
        for sig in (signal.SIGTERM, _proc.SIGKILL):
            for p in cls.procs:
                try:
                    _proc.killpg(p.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(1)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def api(cls, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.op, timeout=60)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        d = r.read()
        c.close()
        return r.status, json.loads(d or b"{}")

    @classmethod
    def wait(cls, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return
            except Exception:
                pass
            time.sleep(0.3)
        raise AssertionError(f"timeout esperando: {what}")

    def load(self, model, slot):
        st, r = self.api("POST", "/api/load", {"model": model, "ctx": 2048, "slot": slot})
        self.assertEqual(st, 202, r)
        k = r["slot"]
        self.wait(lambda: next(s for s in self.api("GET", "/api/state")[1]["slots"] if s["slot"] == k)["state"]
                  in ("READY", "ERROR"), 60, "carga")
        s = next(s for s in self.api("GET", "/api/state")[1]["slots"] if s["slot"] == k)
        self.assertEqual(s["state"], "READY", s["error"])
        return k, s

    def caps(self, model):
        d = self.api("GET", "/v1/models")[1]["data"]
        return next(m for m in d if m["id"] == model)["dllm"]["capabilities"]

    def test_1_tools_template_enables_jinja_and_passes_test(self):
        k, s = self.load("tools.gguf", 0)
        self.assertTrue(s["tools"] and s["tool_template"])
        self.assertIn("tools", self.caps("tools.gguf"))
        st, r = self.api("POST", "/api/tools/test", {"slot": k})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["arguments"], {"city": "Madrid"})
        # además, una petición normal con `tools` llega intacta al modelo a través del proxy /v1
        st, r = self.api("POST", "/v1/chat/completions", {
            "model": "tools.gguf", "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]})
        self.assertEqual(st, 200)
        self.assertEqual(r["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "f")
        self.assertIn("--jinja", r["mock"]["argv"])

    def test_2_template_without_tools_warns_and_fails_test(self):
        k, s = self.load("plain.gguf", "new")
        self.assertTrue(s["tools"] and not s["tool_template"])
        self.assertNotIn("tools", self.caps("plain.gguf"))
        warns = self.api("GET", "/api/events?level=warn")[1]["events"]
        self.assertTrue(any("no parece admitir herramientas" in e["msg"] for e in warns))

    def test_3_no_template_no_jinja_and_off_setting(self):
        k, s = self.load("notpl.gguf", "new")
        self.assertFalse(s["tools"])
        st, r = self.api("POST", "/api/tools/test", {"slot": k})
        self.assertEqual((st, r["ok"]), (200, False))
        self.assertIn("plantilla", r["detail"])
        st, r = self.api("POST", "/api/models/settings", {"model": "tools.gguf", "settings": {"tools": "off"}})
        self.assertEqual(st, 200, r)
        self.assertEqual(self.api("POST", "/api/models/settings", {"model": "tools.gguf", "settings": {"tools": "x"}})[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
