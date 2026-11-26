# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""API compatible con Ollama: traducción de /api/chat, /api/generate, /api/embed, catálogo y seguridad, contra un
orquestador real con llama-server y rpc-server simulados (no se ha probado con clientes reales de Ollama)."""
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
import _proc  # noqa: E402
from dllm import ollama_api  # noqa: E402
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402

GiB = 1 << 30
SECRET = "secreto-ollama-api-1234567890abcd"
TPL_TOOLS = "{% if tools %}{{ tools | tojson }}{% endif %}{% for m in messages %}{{ m.content }}{% endfor %}"


class Translation(unittest.TestCase):
    def test_mensajes_imagenes_y_herramientas(self):
        m = ollama_api.messages_to_oai([
            {"role": "user", "content": "¿qué es?", "images": ["AAAA"]},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}]},
            {"role": "tool", "content": "42", "tool_name": "f"}])
        self.assertEqual(m[0]["content"][1]["image_url"]["url"], "data:image/png;base64,AAAA")
        self.assertEqual(json.loads(m[1]["tool_calls"][0]["function"]["arguments"]), {"a": 1})
        self.assertEqual(m[2]["tool_call_id"], "f")
        self.assertEqual(ollama_api.tool_calls_from_oai([{"function": {"name": "f", "arguments": "{\"x\": 2}"}}]),
                         [{"function": {"name": "f", "arguments": {"x": 2}}}])

    def test_opciones_y_formato(self):
        o = ollama_api.options_to_oai({"temperature": 0.3, "num_predict": 50, "num_ctx": 8192, "stop": ["x"],
                                       "repeat_penalty": 1.1, "num_predict_x": 1})
        self.assertEqual(o, {"temperature": 0.3, "max_tokens": 50, "stop": ["x"], "repeat_penalty": 1.1})
        self.assertEqual(ollama_api.format_to_oai("json"), {"type": "json_object"})
        self.assertEqual(ollama_api.format_to_oai({"type": "object"})["type"], "json_schema")
        self.assertIsNone(ollama_api.format_to_oai(None))
        with self.assertRaises(ollama_api.ApiError):
            ollama_api.messages_to_oai("hola")


@unittest.skipIf(os.name == "nt", "binarios simulados con shebang y grupos de procesos POSIX")
class OllamaE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        models = os.path.join(cls.tmp, "models")
        os.makedirs(os.path.join(models, "ollama"))
        write_gguf(os.path.join(models, "tools.gguf"), 1 * GiB, chat_template=TPL_TOOLS)
        write_gguf(os.path.join(models, "ollama", "qwen2.5-7b.gguf"), 1 * GiB, chat_template=TPL_TOOLS)
        write_gguf(os.path.join(models, "emb.gguf"), GiB // 4)
        cls.procs, cls.op, cls.bp, cls.olp = [], free_port(), free_port(), free_port()
        mb = os.path.join(HERE, "mockbin")
        base = dict(os.environ, PYTHONPATH=ROOT)
        cls.spawn("a", dict(base, DLLM_HOME=f"{cls.tmp}/a"),
                  ["agent", "--secret", SECRET, "--name", "a", "--port", str(free_port()), "--rpc-port",
                   str(free_port()), "--bin-dir", mb, "--backend", "cpu", "--mem-gb", "8", "--bw-gbps", "30",
                   "--announce-to", f"127.0.0.1:{cls.bp}", "--beacon-interval", "0.5"])
        cls.spawn("orch", dict(base, DLLM_HOME=f"{cls.tmp}/o"),
                  ["up", "--secret", SECRET, "--models-dir", models, "--port", str(cls.op), "--bin-dir", mb,
                   "--beacon-port", str(cls.bp), "--node-timeout", "4", "--probe-interval", "0.5",
                   "--ollama-port", str(cls.olp)])
        try:
            cls.wait(lambda: any(n["up"] for n in cls.api("GET", "/api/state")[1]["nodes"]), 40, "nodo")
            for model, slot in (("tools.gguf", 0), ("ollama/qwen2.5-7b.gguf", "new")):
                st, r = cls.api("POST", "/api/load", {"model": model, "ctx": 2048, "slot": slot})
                assert st == 202, r
                cls.wait(lambda: any(s["model"] == model and s["state"] == "READY"
                                     for s in cls.api("GET", "/api/state")[1]["slots"]), 60, "carga " + model)
            cls.api("POST", "/api/models/settings", {"model": "emb.gguf", "settings": {"embeddings": True}})
            st, r = cls.api("POST", "/api/load", {"model": "emb.gguf", "ctx": 512, "slot": "new"})
            assert st == 202, r
            cls.wait(lambda: any(s["model"] == "emb.gguf" and s["state"] == "READY"
                                 for s in cls.api("GET", "/api/state")[1]["slots"]), 60, "carga emb")
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
    def api(cls, method, path, body=None, port=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", port or cls.op, timeout=60)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        r = c.getresponse()
        d = r.read()
        c.close()
        if raw:
            return r.status, d.decode()
        return r.status, json.loads(d or b"{}")

    def ol(self, method, path, body=None, **kw):
        return self.api(method, path, body, port=self.olp, **kw)

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

    def test_catalogo(self):
        st, txt = self.ol("GET", "/", raw=True)
        self.assertEqual((st, txt), (200, "Ollama is running (DLLM)"))
        self.assertEqual(self.ol("GET", "/api/version")[1]["version"], ollama_api.VERSION)
        names = [m["name"] for m in self.ol("GET", "/api/tags")[1]["models"]]
        self.assertIn("tools.gguf", names)
        self.assertIn("ollama/qwen2.5-7b.gguf", names)
        ps = self.ol("GET", "/api/ps")[1]["models"]
        self.assertEqual(ps[0]["details"]["format"], "gguf")
        st, sh = self.ol("POST", "/api/show", {"model": "tools.gguf"})
        self.assertEqual(st, 200, sh)
        self.assertIn("tools", sh["capabilities"])
        self.assertIn("embedding", self.ol("POST", "/api/show", {"model": "emb.gguf"})[1]["capabilities"])
        self.assertEqual(self.ol("POST", "/api/show", {"model": "no-existe"})[0], 404)

    def test_chat_no_stream_opciones_y_nombres(self):
        st, r = self.ol("POST", "/api/chat", {"model": "tools.gguf:latest", "stream": False,
                                              "messages": [{"role": "user", "content": "hola", "images": ["QUJD"]}],
                                              "options": {"temperature": 0.3, "num_predict": 50}, "format": "json"})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["done"])
        self.assertEqual(r["message"], {"role": "assistant", "content": "mock"})
        # nombre al estilo Ollama (qwen2.5:7b) → ollama/qwen2.5-7b.gguf
        st, r = self.ol("POST", "/api/chat", {"model": "qwen2.5:7b", "stream": False,
                                              "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual((st, r["model"]), (200, "ollama/qwen2.5-7b.gguf"))

    def test_chat_stream_ndjson(self):
        st, txt = self.ol("POST", "/api/chat", {"model": "tools.gguf",
                                                "messages": [{"role": "user", "content": "hola"}]}, raw=True)
        self.assertEqual(st, 200)
        lines = [json.loads(x) for x in txt.strip().split("\n")]
        self.assertEqual("".join(x["message"]["content"] for x in lines[:-1]), "tok0 tok1 tok2 ")
        self.assertTrue(lines[-1]["done"])
        self.assertFalse(any(x["done"] for x in lines[:-1]))

    def test_chat_con_herramientas(self):
        tools = [{"type": "function", "function": {"name": "tiempo", "parameters": {"type": "object"}}}]
        st, r = self.ol("POST", "/api/chat", {"model": "tools.gguf", "stream": False, "tools": tools,
                                              "messages": [{"role": "user", "content": "¿tiempo?"}]})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["message"]["tool_calls"], [{"function": {"name": "tiempo", "arguments": {"city": "Madrid"}}}])

    def test_generate(self):
        st, r = self.ol("POST", "/api/generate", {"model": "tools.gguf", "prompt": "hola", "system": "breve",
                                                  "stream": False})
        self.assertEqual((st, r["response"], r["done"]), (200, "mock", True))
        st, txt = self.ol("POST", "/api/generate", {"model": "tools.gguf", "prompt": "hola"}, raw=True)
        lines = [json.loads(x) for x in txt.strip().split("\n")]
        self.assertEqual("".join(x["response"] for x in lines), "tok0 tok1 tok2 ")
        st, r = self.ol("POST", "/api/generate", {"model": "tools.gguf", "prompt": "hola", "raw": True, "stream": False})
        self.assertEqual(r["response"], "mock")
        st, r = self.ol("POST", "/api/generate", {"model": "tools.gguf"})  # sin prompt = cargar
        self.assertEqual((st, r["done_reason"]), (200, "load"))

    def test_embeddings(self):
        st, r = self.ol("POST", "/api/embed", {"model": "emb.gguf", "input": ["a", "b"]})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["embeddings"], [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]])
        st, r = self.ol("POST", "/api/embeddings", {"model": "emb.gguf", "prompt": "a"})
        self.assertEqual(r["embedding"], [0.1, 0.2, 0.3])
        self.assertEqual(self.ol("POST", "/api/embed", {"model": "emb.gguf"})[0], 400)

    def test_seguridad_y_no_admitidas(self):
        st, r = self.ol("POST", "/api/chat", {"model": "tools.gguf", "messages": []},
                        headers={"Origin": "http://web-ajena.example", "X-DLLM-Lang": "en"})
        self.assertEqual(st, 403)
        self.assertEqual(r["error"], "origin not allowed")
        st, r = self.ol("POST", "/api/delete", {"model": "tools.gguf"}, headers={"X-DLLM-Lang": "es"})
        self.assertEqual(st, 501)
        self.assertIn("panel", r["error"])
        self.assertEqual(self.ol("POST", "/api/pull", {})[0], 400)
        self.assertEqual(self.ol("GET", "/api/chat")[0], 405)


if __name__ == "__main__":
    unittest.main()
