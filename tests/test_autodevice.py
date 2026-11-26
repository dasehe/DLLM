# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Autoconfiguración: detección de dispositivos con llama.cpp, elección de GPU, dispositivos remotos en el
orquestador y sincronización automática de la versión de llama.cpp en los nodos.

Escenario e2e (binarios simulados):
  - Nodo «amd»: GPU dedicada AMD (16 GiB) + gráfica integrada, con llama.cpp ANTIGUO (build 2).
  - Nodo «cpu»: sin GPU.
  - Orquestador con GPU propia (CUDA0) que NO debe entrar en el reparto.
Se espera: el nodo amd se actualiza solo a la build del orquestador, presta su GPU dedicada (-d Vulkan0) con su
VRAM real, y llama-server se lanza solo con los dispositivos remotos (--device RPC…)."""
import hashlib
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from dllm import devices, installer  # noqa: E402
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _proc  # noqa: E402

GiB, MiB = 1 << 30, 1 << 20
SECRET = "s3cret-autodevice"


class TestDevicesParse(unittest.TestCase):
    OUT = """ggml_vulkan: Found 2 Vulkan devices:
ggml_vulkan: 0 = AMD Radeon RX 7600 XT (AMD proprietary driver) | uma: 0 | fp16: 1
Available devices:
  Vulkan0: AMD Radeon RX 7600 XT (16368 MiB, 15800 MiB free)
  Vulkan1: AMD Radeon(TM) Graphics (8192 MiB, 8000 MiB free)
  RPC[127.0.0.1:50052]: RPC backend (12288 MiB, 11000 MiB free)
CUDA0: NVIDIA GeForce RTX 3060 (12287 MiB, 11200 MiB free)
"""

    def test_parse_real_format(self):
        d = devices.parse(self.OUT)
        self.assertEqual([x["name"] for x in d], ["Vulkan0", "Vulkan1", "RPC[127.0.0.1:50052]", "CUDA0"])
        self.assertEqual((d[0]["desc"], d[0]["total"], d[0]["free"]), ("AMD Radeon RX 7600 XT", 16368 * MiB, 15800 * MiB))
        self.assertEqual(d[2]["desc"], "RPC backend")  # nombre con ':' y corchetes

    def test_choose(self):
        d = devices.parse(self.OUT)
        self.assertEqual(devices.choose(d)["name"], "Vulkan0")                 # el de más memoria
        self.assertEqual(devices.choose(d, "CUDA0")["name"], "CUDA0")          # por nombre
        self.assertEqual(devices.choose(d, "rtx 3060")["name"], "CUDA0")       # por parte de la descripción
        self.assertIsNone(devices.choose(devices.parse("  CPU: x (1000 MiB, 900 MiB free)")))
        with self.assertRaises(ValueError):
            devices.choose(d, "Metal0")

    def test_remote_diff_keeps_order(self):
        local = devices.parse("  CUDA0: RTX (12287 MiB, 11000 MiB free)")
        both = devices.parse("  CUDA0: RTX (12287 MiB, 11000 MiB free)\n  RPC0[a:1]: RPC (1 MiB, 1 MiB free)\n"
                             "  RPC1[b:2]: RPC (2 MiB, 2 MiB free)")
        self.assertEqual([x["name"] for x in devices.remote(local, both)], ["RPC0[a:1]", "RPC1[b:2]"])


@unittest.skipIf(os.name == "nt", "usa binarios simulados con shebang y grupos de procesos POSIX")
class TestAutoConfigE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.procs, cls.logs = [], []
        # llama.cpp "antiguo" en el nodo amd: los mismos simulados pero con build 2
        cls.oldbin = cls.tmp / "oldbin"
        shutil.copytree(HERE / "mockbin", cls.oldbin)
        (cls.oldbin / "VERSION").write_text("2")
        cls.release = cls._make_release("b1")  # la build del orquestador (simulados sin VERSION → 1)
        cls.models = cls.tmp / "models"
        cls.models.mkdir()
        write_gguf(str(cls.models / "m.gguf"), 15 * GiB)  # no cabe solo en la GPU AMD (~13 GiB útiles)
        cls.bp, cls.op = free_port(), free_port()
        cls.args_file = cls.tmp / "amd-rpc-args.json"
        base = dict(os.environ, PYTHONPATH=str(ROOT), DLLM_RELEASE_FILE=str(cls.release))
        cls.amd = cls._spawn("amd", dict(base, DLLM_HOME=str(cls.tmp / "amd"), MOCK_ARGS_FILE=str(cls.args_file),
                                         MOCK_DEVICES="Vulkan0|AMD Radeon RX 7600 XT|16368|15800;"
                                                      "Vulkan1|AMD Radeon(TM) Graphics|8192|8000"),
                             ["agent", "--secret", SECRET, "--name", "amd", "--port", str(free_port()),
                              "--rpc-port", str(free_port()), "--bin-dir", str(cls.oldbin), "--llama-backend", "cpu",
                              "--announce-to", f"127.0.0.1:{cls.bp}", "--beacon-interval", "0.5"])
        cls._spawn("cpu", dict(base, DLLM_HOME=str(cls.tmp / "cpu")),
                   ["agent", "--secret", SECRET, "--name", "cpu", "--port", str(free_port()),
                    "--rpc-port", str(free_port()), "--bin-dir", str(HERE / "mockbin"), "--backend", "cpu",
                    "--mem-gb", "4", "--bw-gbps", "30", "--announce-to", f"127.0.0.1:{cls.bp}",
                    "--beacon-interval", "0.5"])
        cls._spawn("orch", dict(base, DLLM_HOME=str(cls.tmp / "orch"),
                                MOCK_LOCAL_DEVICES="CUDA0|NVIDIA GeForce RTX 3060 (orquestador)|12287|11000"),
                   ["up", "--secret", SECRET, "--models-dir", str(cls.models), "--port", str(cls.op),
                    "--bin-dir", str(HERE / "mockbin"), "--beacon-port", str(cls.bp), "--node-timeout", "4",
                    "--probe-interval", "0.5"])
        try:
            cls.wait(lambda: len([n for n in cls.state()["nodes"] if n["up"]]) == 2, 40, "2 nodos")
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def _make_release(cls, tag):
        info = installer.platform_info()
        name = (f"llama-{tag}-bin-macos-{info['arch']}.tar.gz" if info["os"] == "macos"
                else f"llama-{tag}-bin-ubuntu-{info['arch']}.tar.gz")
        arc = cls.tmp / name
        with tarfile.open(arc, "w:gz") as t:
            # como las releases actuales de llama.cpp: el servidor RPC se llama ggml-rpc-server
            for b, dst in (("llama-server", "llama-server"), ("rpc-server", "ggml-rpc-server")):
                t.add(HERE / "mockbin" / b, arcname=f"build/bin/{dst}")
        rel = cls.tmp / "release.json"
        rel.write_text(json.dumps({"tag_name": tag, "assets": [{
            "name": name, "browser_download_url": arc.as_uri(), "size": arc.stat().st_size,
            "digest": "sha256:" + hashlib.sha256(arc.read_bytes()).hexdigest()}]}))
        return rel

    @classmethod
    def _spawn(cls, name, env, args):
        log = open(cls.tmp / f"{name}.log", "w")
        p = subprocess.Popen([sys.executable, "-m", "dllm"] + args, cwd=str(ROOT), env=env, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        cls.procs.append(p)
        return p

    @classmethod
    def tearDownClass(cls):
        for sig in (signal.SIGTERM, _proc.SIGKILL):
            for p in cls.procs:
                try:
                    _proc.killpg(p.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(0.5)
        if not getattr(cls, "_cleaned", False):
            cls._cleaned = True
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.op, timeout=60)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        d = r.read()
        c.close()
        return r.status, json.loads(d or b"{}")

    @classmethod
    def state(cls):
        return cls.req("GET", "/api/state")[1]

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
        logs = "\n".join(f"--- {f.name}\n{f.read_text()[-2500:]}" for f in sorted(cls.tmp.glob("*.log")))
        raise AssertionError(f"timeout esperando: {what}\n{logs}")

    def node(self, name):
        return next(n for n in self.state()["nodes"] if n["name"] == name)

    def test_1_agent_syncs_llama_build_by_itself(self):
        self.wait(lambda: self.node("amd")["rpc_build"] == "1 (mock)", 60, "actualización automática de llama.cpp")
        n = self.node("amd")
        self.assertEqual(n["update"]["state"], "completada")
        self.assertEqual(n["update"]["tag"], "b1")

    def test_2_gpu_detected_with_real_vram(self):
        n = self.node("amd")
        self.assertEqual((n["backend"], n["device"], n["device_desc"]), ("gpu", "Vulkan0", "AMD Radeon RX 7600 XT"))
        self.assertEqual(len(n["devices"]), 2)
        # recomendado: min(libre − reserva, 75 % del total) de la VRAM, no de la RAM del equipo
        self.assertEqual(n["cap_bytes"], min(15800 * MiB - GiB, int(16368 * MiB * 0.95)))
        self.assertEqual(self.node("cpu")["backend"], "cpu")

    def test_3_load_uses_only_remote_devices(self):
        self.wait(lambda: self.node("amd")["rpc_build"] == "1 (mock)", 60, "nodo amd actualizado")
        st, r = self.req("POST", "/api/load", {"model": "m.gguf", "ctx": 2048})
        self.assertEqual(st, 202, r)
        self.wait(lambda: self.state()["state"] in ("READY", "ERROR"), 60, "carga")
        s = self.state()
        self.assertEqual(s["state"], "READY", s.get("error"))
        self.assertEqual([a["name"] for a in s["plan"]["assignments"]], ["amd", "cpu"])
        st, r = self.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hola"}]})
        info = r["mock"]
        used = info["device"].split(",")
        self.assertEqual(len(used), 2)
        self.assertTrue(all(u.startswith("RPC") for u in used), used)   # la GPU local del orquestador no entra
        self.assertEqual(len(info["ts"].split(",")), 2)
        self.assertEqual(info["echo_ok"], [True, True])
        # el rpc-server del nodo amd presta solo su GPU dedicada
        args = json.loads(self.args_file.read_text())
        self.assertEqual(args[args.index("-d") + 1], "Vulkan0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
