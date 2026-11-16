# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Pruebas del instalador: unitarias (assets, extracción segura, sha256, servicios, código de unión) y de
extremo a extremo usando el .pyz REAL con una 'release' local falsa que contiene los binarios simulados.

Dos "equipos" (orquestador+nodo y nodo) conviven en esta máquina con HOME/DLLM_HOME y puertos distintos."""
import hashlib
import http.client
import io
import json
import os
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from dllm import config, hw, installer, wizard  # noqa: E402
from make_gguf import write_gguf  # noqa: E402
from ports import free_port  # noqa: E402

GiB = 1 << 30
ASSETS = [{"name": n, "url": "x", "size": 1, "sha256": None} for n in [
    "llama-b9999-bin-ubuntu-x64.tar.gz", "llama-b9999-bin-ubuntu-vulkan-x64.tar.gz",
    "llama-b9999-bin-ubuntu-arm64.tar.gz", "llama-b9999-bin-ubuntu-rocm-7.2-x64.tar.gz",
    "llama-b9999-bin-macos-arm64.tar.gz", "llama-b9999-bin-macos-arm64-kleidiai.tar.gz",
    "llama-b9999-bin-macos-x64.tar.gz", "llama-b9999-bin-win-cpu-x64.zip", "llama-b9999-bin-win-vulkan-x64.zip",
    "llama-b9999-bin-win-cuda-12.4-x64.zip", "llama-b9999-bin-win-cuda-13.1-x64.zip",
    "cudart-llama-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-13.1-x64.zip",
    # nombres reales de b12380 (nov. 2026)
    "llama-b9999-bin-ubuntu-cuda-12.8-x64.tar.gz", "cudart-llama-b9999-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "llama-b9999-bin-ubuntu-cuda-13.5-x64.tar.gz", "cudart-llama-b9999-bin-ubuntu-cuda-13.5-x64.tar.gz",
    "llama-b9999-bin-ubuntu-sycl-fp16-x64.tar.gz", "llama-b9999-bin-win-rocm-10.0-x64.zip",
    "llama-b9999-bin-win-sycl-x64.zip"]]


def names(lst):
    return [a["name"] for a in lst]


class TestJoinCode(unittest.TestCase):
    d = {"secret": "s3cr3t-con-guiones_y_raros", "orch": "192.168.1.10", "beacon": 50999, "tag": "b1234"}

    def test_roundtrip_and_whitespace(self):
        code = config.encode_join(self.d)
        self.assertTrue(code.startswith("DLLM1-"))
        spaced = " " + code[:20] + "\n" + code[20:] + " \n"  # como si se hubiera copiado con saltos de línea
        self.assertEqual(config.decode_join(spaced)["secret"], self.d["secret"])

    def test_detects_typos_and_garbage(self):
        code = config.encode_join(self.d)
        for bad in (code[:-3], code[:12] + "X" + code[13:], "hola", "", "DLLM1-", "DLLM1-abc-00000000"):
            with self.assertRaises(ValueError):
                config.decode_join(bad)


class TestAssets(unittest.TestCase):
    def pick(self, *a, **k):
        return names(installer.pick_assets(ASSETS, *a, **k))

    def test_linux(self):
        self.assertEqual(self.pick("linux", "x64", "cpu"), ["llama-b9999-bin-ubuntu-x64.tar.gz"])
        self.assertEqual(self.pick("linux", "x64", "vulkan"), ["llama-b9999-bin-ubuntu-vulkan-x64.tar.gz"])
        self.assertEqual(self.pick("linux", "x64", "rocm"), ["llama-b9999-bin-ubuntu-rocm-7.2-x64.tar.gz"])
        self.assertEqual(self.pick("linux", "arm64", "cpu"), ["llama-b9999-bin-ubuntu-arm64.tar.gz"])
        self.assertEqual(self.pick("linux", "x64", "cuda", cuda="13.0"),  # la más alta que admite el driver
                         ["llama-b9999-bin-ubuntu-cuda-12.8-x64.tar.gz",
                          "cudart-llama-b9999-bin-ubuntu-cuda-12.8-x64.tar.gz"])
        self.assertEqual(self.pick("linux", "x64", "cuda", cuda="13.5")[0], "llama-b9999-bin-ubuntu-cuda-13.5-x64.tar.gz")
        self.assertEqual(self.pick("linux", "x64", "sycl"), ["llama-b9999-bin-ubuntu-sycl-fp16-x64.tar.gz"])
        with self.assertRaises(installer.InstallError):  # driver demasiado antiguo
            self.pick("linux", "x64", "cuda", cuda="12.2")

    def test_macos_ignores_kleidiai(self):
        self.assertEqual(self.pick("macos", "arm64", "metal"), ["llama-b9999-bin-macos-arm64.tar.gz"])
        self.assertEqual(self.pick("macos", "x64", "cpu"), ["llama-b9999-bin-macos-x64.tar.gz"])

    def test_windows_overlays(self):
        self.assertEqual(self.pick("windows", "x64", "cpu"), ["llama-b9999-bin-win-cpu-x64.zip"])
        self.assertEqual(self.pick("windows", "x64", "vulkan"),
                         ["llama-b9999-bin-win-cpu-x64.zip", "llama-b9999-bin-win-vulkan-x64.zip"])
        self.assertEqual(self.pick("windows", "x64", "cuda", cuda="12.8"),
                         ["llama-b9999-bin-win-cpu-x64.zip", "llama-b9999-bin-win-cuda-12.4-x64.zip",
                          "cudart-llama-bin-win-cuda-12.4-x64.zip"])
        self.assertIn("llama-b9999-bin-win-cuda-13.1-x64.zip", self.pick("windows", "x64", "cuda", cuda="13.2"))
        with self.assertRaises(installer.InstallError):  # driver demasiado antiguo
            self.pick("windows", "x64", "cuda", cuda="11.8")
        self.assertEqual(self.pick("windows", "x64", "rocm")[1], "llama-b9999-bin-win-rocm-10.0-x64.zip")
        self.assertEqual(self.pick("windows", "x64", "sycl")[1], "llama-b9999-bin-win-sycl-x64.zip")

    def test_missing_asset_lists_available(self):
        with self.assertRaises(installer.InstallError) as c:
            installer.pick_assets(ASSETS[:2], "macos", "arm64", "metal")
        self.assertIn("Disponibles", str(c.exception))

    def test_backend_suggestion(self):
        base = {"os": "linux", "nvidia": [], "other_gpu": [], "cuda": None}
        self.assertEqual(installer.suggest_backends(base)[0], "cpu")
        self.assertEqual(installer.suggest_backends(dict(base, nvidia=[{"name": "x"}]))[0], "vulkan")
        self.assertEqual(installer.suggest_backends(dict(base, nvidia=[{"name": "x"}], cuda="12.8"))[0], "cuda")
        self.assertEqual(installer.suggest_backends(dict(base, other_gpu=["AMD Radeon"]))[0], "vulkan")
        self.assertEqual(installer.fallback_backends("cuda", dict(base, nvidia=[{"name": "x"}], cuda="12.8")),
                         ["cuda", "vulkan", "cpu"])
        self.assertEqual(installer.fallback_backends("cpu", base), ["cpu"])
        self.assertEqual(installer.suggest_backends(dict(base, os="windows", nvidia=[{"name": "x"}], cuda="12.6"))[0], "cuda")
        self.assertEqual(installer.suggest_backends(dict(base, os="windows", nvidia=[{"name": "x"}], cuda="11.8"))[0], "vulkan")
        self.assertEqual(installer.suggest_backends(dict(base, os="macos", arch="arm64"))[0], "metal")


def _rel(tag, names, **kw):
    return dict({"tag_name": tag, "assets": [{"name": n.format(t=tag), "browser_download_url": "x", "size": 1}
                                             for n in names]}, **kw)


FULL = ["llama-{t}-bin-win-cpu-x64.zip", "llama-{t}-bin-win-vulkan-x64.zip", "llama-{t}-bin-win-cuda-12.4-x64.zip",
        "llama-{t}-bin-ubuntu-x64.tar.gz", "llama-{t}-bin-ubuntu-vulkan-x64.tar.gz",
        "llama-{t}-bin-ubuntu-cuda-12.8-x64.tar.gz", "llama-{t}-bin-macos-arm64.tar.gz"]


class TestReleaseChoice(unittest.TestCase):
    def test_skips_non_builds_drafts_and_incomplete(self):
        # caso real (nov. 2026): v1.0.0 es «Latest» sin binarios y todas las bNNNN están marcadas como pre-release
        rels = [_rel("v1.0.0", ["nightly-tag.txt"]),
                _rel("b12380", [n for n in FULL if "win-vulkan" not in n], prerelease=True),  # incompleta
                _rel("b12381", FULL, draft=True, prerelease=True),
                _rel("b12379", FULL, prerelease=True), _rel("b12378", FULL, prerelease=True)]
        self.assertEqual(installer.choose_release(rels)["tag"], "b12379")

    def test_none_complete(self):
        with self.assertRaises(installer.InstallError):
            installer.choose_release([_rel("b1", FULL[:2]), _rel("v1.0.0", [])])

    def test_pinned_tag_without_package_explains_what_to_do(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "rels.json"
            f.write_text(json.dumps([_rel("b7", ["llama-{t}-bin-win-cpu-x64.zip"])]))
            info = dict(installer.platform_info(), os="windows", arch="x64")
            with mock.patch.dict(os.environ, {"DLLM_RELEASE_FILE": str(f)}):
                with self.assertRaises(installer.InstallError) as c:
                    installer.install_llama(tmp / "llama", "b7", "vulkan", info, lambda *a: None)
            self.assertIn("la fija el orquestador", str(c.exception))
            self.assertIn("Vulkan", str(c.exception))
            with mock.patch.dict(os.environ, {"DLLM_RELEASE_FILE": str(f)}):
                with self.assertRaises(installer.InstallError):
                    installer.fetch_release("b999")
        finally:
            shutil.rmtree(tmp)


class TestFolderPicker(unittest.TestCase):
    def test_browse_and_mkdir(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "Modelos").mkdir()
            (tmp / ".oculta").mkdir()
            (tmp / "fichero.txt").write_text("x")
            r = wizard.browse_dir(str(tmp))
            self.assertEqual(r["dirs"], ["Modelos"])          # solo carpetas, sin ocultas
            self.assertIsNotNone(r["free"])
            self.assertEqual(wizard.browse_dir(str(tmp / "no" / "existe"))["path"], str(tmp.resolve()))
            r = wizard.make_dir(str(tmp), "dllm")
            self.assertTrue((tmp / "dllm").is_dir())
            for bad in ("", "..", "a/b", "a\\b"):
                with self.assertRaises(ValueError):
                    wizard.make_dir(str(tmp), bad)
        finally:
            shutil.rmtree(tmp)

    def test_windows_picker_script(self):
        # la ruta y el título viajan por variables de entorno, nunca dentro del script
        self.assertIn("$env:DLLM_PICK_INIT", wizard._WIN_PICK)
        self.assertIn("TopMost=$true", wizard._WIN_PICK)
        self.assertIn("$w.Show()", wizard._WIN_PICK)
        if os.name == "nt":  # sintaxis de PowerShell válida (se analiza sin ejecutarlo: no abre ninguna ventana)
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "[void][scriptblock]::Create($env:S); 'ok'"],
                               env=dict(os.environ, S=wizard._WIN_PICK), capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.strip(), "ok", r.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "sin escritorio solo se puede comprobar en Linux")
    def test_no_desktop_falls_back_to_in_page_browser(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(wizard.native_pick_dir(), {"native": False})


class TestRpcServerName(unittest.TestCase):
    """llama.cpp renombró rpc-server a ggml-rpc-server: se aceptan los dos nombres."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, *names):
        d = self.tmp / "build" / "bin"
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            f = d / (n + installer.EXE)
            f.write_text("#!/bin/sh\n")
            f.chmod(0o755)
        return d

    def test_new_name(self):
        d = self._touch("llama-server", "ggml-rpc-server")
        self.assertEqual(installer.locate_bin(self.tmp), d)
        self.assertEqual(installer.bin_path(d, "rpc-server").name, "ggml-rpc-server" + installer.EXE)
        self.assertEqual(Path(hw.find_bin("rpc-server", str(d))).name, "ggml-rpc-server" + installer.EXE)

    def test_old_name_and_missing(self):
        d = self._touch("llama-server", "rpc-server")
        self.assertEqual(installer.locate_bin(self.tmp, ("rpc-server",)), d)
        self.assertEqual(Path(hw.find_bin("rpc-server", str(d))).name, "rpc-server" + installer.EXE)
        (d / ("rpc-server" + installer.EXE)).unlink()
        self.assertIsNone(installer.locate_bin(self.tmp))
        self.assertEqual(installer.locate_bin(self.tmp, ("llama-server",)), d)
        with self.assertRaisesRegex(installer.InstallError, "ggml-rpc-server"):
            installer.verify_bins(d, ("rpc-server",))


    @unittest.skipIf(os.name == "nt", "simulados con shebang")
    def test_rpc_server_without_version_flag(self):
        # el rpc-server real no admite --version: se comprueba con --help y la build sale de llama-server
        d = self.tmp / "bin"
        d.mkdir()
        shutil.copy(HERE / "mockbin" / "llama-server", d / "llama-server")
        shutil.copy(HERE / "mockbin" / "rpc-server", d / "ggml-rpc-server")
        (d / "VERSION").write_text("12387")
        ok, out = installer.run_check(d / "ggml-rpc-server")
        self.assertTrue(ok, out)
        self.assertEqual(hw.build_id(str(d / "ggml-rpc-server")), "12387 (mock)")
        self.assertEqual(installer.verify_bins(d, ("rpc-server",)), "12387 (mock)")
        (d / "llama-server").unlink()  # solo el servidor RPC: arranca, pero no se sabe su build
        self.assertTrue(installer.run_check(d / "ggml-rpc-server")[0])
        self.assertIsNone(hw.build_id(str(d / "ggml-rpc-server")))


class TestBuildFallback(unittest.TestCase):
    def test_build_from_folder_and_compare(self):
        from dllm import orchestrator
        tmp = Path(tempfile.mkdtemp())
        try:
            d = tmp / "llama-b12387-vulkan"
            d.mkdir()
            f = d / ("ggml-rpc-server" + installer.EXE)
            f.write_text("")
            with mock.patch.object(hw, "_version_of", return_value=None):
                self.assertEqual(hw.build_id(str(f)), "12387 (?)")
        finally:
            shutil.rmtree(tmp)
        self.assertTrue(orchestrator.same_build("12387 (?)", "12387 (a1b2c3)"))
        self.assertFalse(orchestrator.same_build("12386 (x)", "12387 (x)"))
        hw.hide_child_consoles()  # en Linux/macOS no hace nada


class TestWindowsBlocked(unittest.TestCase):
    """Smart App Control bloquea las DLL sin firmar: el error debe explicar qué hacer, no colgarse."""

    def test_explain_exit(self):
        self.assertIn("Control inteligente", installer.explain_exit(0xC0E90002))
        self.assertIn("Control inteligente", installer.explain_exit(-1058471934))  # mismo código con signo
        self.assertIsNone(installer.explain_exit(1))
        self.assertIsNone(installer.explain_exit(0))

    def test_run_check_reports_block(self):
        with mock.patch.object(installer.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0xC0E90002, "", "")):
            ok, txt = installer.run_check("llama-server")
        self.assertFalse(ok)
        self.assertIn("Smart App Control", txt)

    def test_timeout_mentions_sac_only_if_enabled(self):
        boom = subprocess.TimeoutExpired("llama-server", 60)
        with mock.patch.object(installer.subprocess, "run", side_effect=boom):
            with mock.patch.object(hw, "smart_app_control", return_value="on"):
                self.assertIn("Smart App Control", installer.run_check("x")[1])
            with mock.patch.object(hw, "smart_app_control", return_value=None):
                self.assertNotIn("Smart App Control", installer.run_check("x")[1])

    def test_winerror_4551_es_bloqueo_de_politica(self):
        e = OSError(); e.winerror = 4551
        with mock.patch.object(installer.subprocess, "run", side_effect=e):
            ok, txt = installer.run_check("x")
        self.assertFalse(ok)
        self.assertIn("Control inteligente", txt)
        with mock.patch.object(installer, "run_check", return_value=(False, installer.POLICY_HINT)), \
                mock.patch.object(installer, "bin_path", return_value=Path(__file__)):
            with self.assertRaises(installer.PolicyBlocked):
                installer.verify_bins("d", ("llama-server",))

    def test_sac_state_off_windows(self):
        if os.name != "nt":
            self.assertIsNone(hw.smart_app_control())
        else:
            self.assertIn(hw.smart_app_control(), ("on", "eval", "off", None))
        hw.quiet_windows_errors()  # no falla en ningún sistema


class TestFirewallAndFolders(unittest.TestCase):
    def test_firewall_plan_minimal(self):
        cfg = config.default("node")
        cfg["agent"]["allow_ip"] = ["192.168.1.10", "127.0.0.1"]
        self.assertEqual(installer.firewall_plan(cfg), [("DLLM-agente", "TCP", "8765,50051", "192.168.1.10")])
        cfg["agent"]["allow_ip"] = []
        self.assertEqual(installer.firewall_plan(cfg)[0][3], "LocalSubnet")
        both = config.default("both")
        both["agent"]["allow_ip"] = ["127.0.0.1"]
        self.assertEqual(installer.firewall_plan(both), [("DLLM-beacon", "UDP", "50999", "LocalSubnet")])
        both["orchestrator"]["host"] = "0.0.0.0"
        self.assertIn(("DLLM-API", "TCP", "8080", "LocalSubnet"), installer.firewall_plan(both))
        both["advertise_host"] = "10.8.0.1"  # por VPN los nodos no están en la subred local
        self.assertTrue(all(r[3] == "any" for r in installer.firewall_plan(both)))

    def test_netsh_rules_scoped_to_program(self):
        cfg = config.default("node")
        rules = installer.netsh_rules(cfg, programs=[r"C:\Py\pythonw.exe", r"C:\Py\python.exe"])
        self.assertEqual(len(rules), 2)
        self.assertIn(r"program=C:\Py\pythonw.exe", rules[0])
        self.assertIn("remoteip=LocalSubnet", rules[0])
        script = installer.firewall_script(cfg, programs=[r"C:\Mi Python\it's\pythonw.exe"])
        self.assertIn("delete rule 'name=DLLM-agente'", script)
        self.assertIn("it''s", script)  # comillas escapadas para PowerShell

    def test_open_firewall_off_windows_does_nothing(self):
        if os.name == "nt":
            self.skipTest("en Windows pediría permiso de administrador")
        r = installer.open_firewall(config.default("node"))
        self.assertFalse(r["auto"])

    def test_install_dir_and_uninstall(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            with mock.patch.dict(os.environ, {"DLLM_HOME": str(tmp / "home")}):
                sp = wizard.Setup(dry_run=True)
                n = sp.normalize({"role": "orchestrator", "install_dir": str(tmp / "DLLM")})
                self.assertEqual(n["models_dir"], str(tmp / "DLLM" / "models"))
                n = sp.normalize({"role": "both", "install_dir": str(tmp / "DLLM"), "models_dir": str(tmp / "m")})
                self.assertEqual(n["models_dir"], str(tmp / "m"))
                cfg = config.default("orchestrator")
                cfg.update(install_dir=str(tmp / "DLLM"), models_dir=str(tmp / "DLLM" / "models"))
                self.assertEqual(config.llama_dir(cfg), tmp / "DLLM" / "llama.cpp")
                self.assertEqual(config.llama_dir({}), config.home() / "llama")
                for d in (tmp / "DLLM" / "llama.cpp" / "b1", tmp / "DLLM" / "models", tmp / "home"):
                    d.mkdir(parents=True)
                (tmp / "DLLM" / "models" / "a.gguf").write_text("x")
                with mock.patch.object(installer, "uninstall_service"), mock.patch.object(installer, "close_firewall"):
                    done = installer.uninstall_all(cfg, purge=True, models=False)
                    self.assertFalse((tmp / "DLLM" / "llama.cpp").exists())
                    self.assertFalse((tmp / "home").exists())
                    self.assertTrue((tmp / "DLLM" / "models" / "a.gguf").exists(), done)
                    installer.uninstall_all(cfg, purge=True, models=True)
                    self.assertFalse((tmp / "DLLM").exists())
                    # una carpeta de modelos ajena a DLLM nunca se borra
                    (tmp / "suyos").mkdir()
                    cfg["models_dir"] = str(tmp / "suyos")
                    done = installer.uninstall_all(cfg, purge=True, models=True)
                    self.assertTrue((tmp / "suyos").exists())
                    self.assertTrue(any("conservados" in x for x in done))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPresence(unittest.TestCase):
    def test_presence_tracking(self):
        from dllm import presence
        p = presence.Presence(timeout=0.3)
        self.assertEqual(p.view(), {"active": 0, "seen": False})
        p.touch("abc123")
        p.touch("../mal")  # id no válido: se ignora
        self.assertEqual(p.view(), {"active": 1, "seen": True})
        p.bye("abc123")
        self.assertEqual(p.view()["active"], 0)
        p.touch("zzz999")
        time.sleep(0.4)
        self.assertEqual(p.view()["active"], 0)  # sin avisos: se da por cerrada

    def test_watch_reopens_and_stops(self):
        from dllm import presence
        views = iter([{"active": 1, "seen": True}] + [{"active": 0, "seen": True}] * 10)
        calls, out = [], []
        with mock.patch.object(presence, "BYE_GRACE", 0), mock.patch.object(presence.time, "sleep"):
            r = presence.watch(lambda: next(views), lambda: calls.append("open"), stop=lambda: calls.append("stop"),
                               ask=lambda _q, it=iter(["a", "p"]): next(it), out=out.append)
        self.assertEqual((r, calls), ("parado", ["open", "stop"]))
        self.assertTrue(any("sigue en marcha" in x for x in out))
        r = presence.watch(lambda: None, lambda: None)
        self.assertEqual(r, "terminado")


class TestSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tar_traversal_blocked(self):
        p = self.tmp / "evil.tar.gz"
        with tarfile.open(p, "w:gz") as t:
            ti = tarfile.TarInfo("../evil.txt")
            ti.size = 1
            t.addfile(ti, io.BytesIO(b"x"))
        with self.assertRaises(installer.InstallError):
            installer.safe_extract(p, self.tmp / "out")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_tar_symlink_escape_blocked(self):
        p = self.tmp / "link.tar.gz"
        with tarfile.open(p, "w:gz") as t:
            ti = tarfile.TarInfo("lib.so")
            ti.type, ti.linkname = tarfile.SYMTYPE, "../../etc/passwd"
            t.addfile(ti)
        with self.assertRaises(installer.InstallError):
            installer.safe_extract(p, self.tmp / "out")

    def test_zip_traversal_blocked(self):
        p = self.tmp / "evil.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("../evil.txt", "x")
        with self.assertRaises(installer.InstallError):
            installer.safe_extract(p, self.tmp / "out")

    @unittest.skipIf(os.name == "nt", "bit ejecutable y symlinks son POSIX")
    def test_normal_extract_keeps_exec_bit_and_symlinks(self):
        p = self.tmp / "ok.tar.gz"
        src = self.tmp / "bin"
        src.write_text("#!/bin/sh\n")
        src.chmod(0o755)
        with tarfile.open(p, "w:gz") as t:
            t.add(src, arcname="build/bin/llama-server")
            ti = tarfile.TarInfo("build/bin/libx.so")
            ti.type, ti.linkname = tarfile.SYMTYPE, "libx.so.1"
            t.addfile(ti)
        installer.safe_extract(p, self.tmp / "out")
        self.assertTrue(os.access(self.tmp / "out/build/bin/llama-server", os.X_OK))
        self.assertTrue((self.tmp / "out/build/bin/libx.so").is_symlink())

    def test_sha256(self):
        f = self.tmp / "a.bin"
        f.write_bytes(b"hola")
        good = hashlib.sha256(b"hola").hexdigest()
        self.assertTrue(installer.download(f.as_uri(), self.tmp / "b.bin", good))
        with self.assertRaises(installer.InstallError):
            installer.download(f.as_uri(), self.tmp / "c.bin", "0" * 64)
        self.assertFalse((self.tmp / "c.bin").exists())  # el archivo corrupto se descarta
        self.assertFalse(installer.download(f.as_uri(), self.tmp / "d.bin", None))  # sin hash publicado


class TestServices(unittest.TestCase):
    def test_systemd(self):
        u = installer.systemd_unit(["/usr/bin/python3", "/home/u x/.dllm/dllm.pyz", "run"], {"PYTHONPATH": "/a b"})
        self.assertIn("ExecStart=/usr/bin/python3 '/home/u x/.dllm/dllm.pyz' run", u)
        self.assertIn("Restart=always", u)
        self.assertIn("NoNewPrivileges=true", u)
        self.assertNotIn("secret", u.lower())  # el secreto vive en config.json (600), no en la unidad

    def test_launchd_and_schtasks(self):
        pl = plistlib.loads(installer.launchd_plist(["/usr/bin/python3", "/x/dllm.pyz", "run"]))
        self.assertEqual((pl["Label"], pl["RunAtLoad"], pl["KeepAlive"]), ("com.dllm", True, True))
        c = installer.schtasks_create(["C:\\Python\\pythonw.exe", "C:\\Users\\a b\\dllm.pyz", "run"])
        self.assertIn("ONLOGON", c)
        self.assertIn('"C:\\Users\\a b\\dllm.pyz"', c[c.index("/TR") + 1])

    def test_firewall_hints_restrict_source(self):
        cfg = config.default("node")
        cfg["agent"]["allow_ip"] = ["192.168.1.10", "127.0.0.1"]
        if sys.platform.startswith("linux"):
            h = installer.firewall_hints(cfg)
            self.assertTrue(all("192.168.1.10" in c for c in h) and len(h) == 2)


class TestDryRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            os.environ["DLLM_HOME"] = t + "/h"
            try:
                r = wizard.Setup(dry_run=True).install({"role": "orchestrator", "backend": "cpu", "autostart": False,
                                                        "models_dir": t})
                self.assertTrue(r["join_code"].startswith("DLLM1-"))
                self.assertFalse(os.path.exists(t + "/h"))
            finally:
                del os.environ["DLLM_HOME"]

    def test_advertise_host_goes_into_join_code(self):
        r = wizard.Setup(dry_run=True).install({"role": "both", "backend": "cpu", "autostart": False,
                                                "advertise_host": "10.8.0.1"})
        self.assertEqual(config.decode_join(r["join_code"])["orch"], "10.8.0.1")

    def test_contribution_mode(self):
        s = wizard.Setup(dry_run=True)
        self.assertEqual(s.normalize({"role": "both"})["limits_mode"], "auto")
        self.assertEqual(s.normalize({"role": "both", "share": 33})["limits_mode"], "manual")  # fijar ⇒ manual
        self.assertEqual(s.normalize({"role": "both", "share": 33, "limits_mode": "auto"})["limits_mode"], "auto")
        for bad in ({"share": 0}, {"share": 101}, {"threads": 0}, {"threads": 2.5}, {"limits_mode": "x"}):
            with self.assertRaises(ValueError):
                s.normalize(dict(bad, role="both"))

    def test_validation_messages(self):
        s = wizard.Setup(dry_run=True)
        for bad, frag in (({}, "elige"), ({"role": "node", "join_code": "DLLM1-zz-00"}, "código"),
                          ({"role": "both", "api_port": "99999"}, "rango"), ({"role": "both", "backend": "x"}, "backend")):
            with self.assertRaises(ValueError) as c:
                s.normalize(bad)
            self.assertIn(frag, str(c.exception))


# =====================================================================================
@unittest.skipUnless(sys.platform.startswith("linux"), "e2e del instalador probado en Linux")
class TestInstallE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_package
        cls.tmp = Path(tempfile.mkdtemp())
        pkg, _ = build_package.build(cls.tmp / "dist")
        cls.pyz = pkg / "dllm.pyz"
        cls.procs = []
        cls.release = cls.make_release(cls.tmp, "b9999")

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                time.sleep(0.5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def make_release(tmp, tag, bad_digest=False):
        info = installer.platform_info()
        name = f"llama-{tag}-bin-ubuntu-{info['arch']}.tar.gz"
        arc = tmp / name
        with tarfile.open(arc, "w:gz") as t:  # estructura real de las releases: build/bin/…
            # como las releases actuales de llama.cpp: el servidor RPC se llama ggml-rpc-server
            for b, dst in (("llama-server", "llama-server"), ("rpc-server", "ggml-rpc-server")):
                t.add(HERE / "mockbin" / b, arcname=f"build/bin/{dst}")
        digest = "0" * 64 if bad_digest else hashlib.sha256(arc.read_bytes()).hexdigest()
        rel = tmp / ("release-bad.json" if bad_digest else "release.json")
        rel.write_text(json.dumps({"tag_name": tag, "assets": [{
            "name": name, "browser_download_url": arc.as_uri(), "size": arc.stat().st_size,
            "digest": "sha256:" + digest}]}))
        return rel

    def env(self, home, release=None):
        h = self.tmp / home
        h.mkdir(exist_ok=True)
        return dict(os.environ, HOME=str(h), DLLM_HOME=str(h / ".dllm"),
                    DLLM_RELEASE_FILE=str(release or self.release), PYTHONDONTWRITEBYTECODE="1")

    def run_pyz(self, env, *args, check=True, timeout=120):
        r = subprocess.run([sys.executable, str(self.pyz), *args], env=env, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        if check and r.returncode:
            self.fail(f"{args} → {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def spawn(self, env, *args):
        log = open(self.tmp / f"proc-{len(self.procs)}.log", "w")
        p = subprocess.Popen([sys.executable, *args], env=env, cwd=self.tmp, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(p)
        return p

    def logs(self):
        return "\n".join(f"--- {f.name}\n{f.read_text()[-3000:]}" for f in sorted(self.tmp.glob("proc-*.log")))

    def http(self, port, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)

    def wait(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return
            except Exception:
                pass
            time.sleep(0.4)
        self.fail(f"timeout esperando: {what}\n{self.logs()}")

    # ------------------------------------------------------------------ pruebas
    def test_1_full_cluster_from_installer(self):
        p_api, p_beacon = free_port(), free_port()
        pa1, ra1, pa2, ra2 = free_port(), free_port(), free_port(), free_port()
        models = self.tmp / "models"
        models.mkdir()
        write_gguf(str(models / "mock.gguf"), 6 * GiB)

        # --- equipo A: orquestador + nodo, instalado con el .pyz
        envA = self.env("A")
        r = self.run_pyz(envA, "setup", "--tui", "--yes", "--role", "both", "--backend", "cpu", "--name", "equipoA",
                         "--models-dir", str(models), "--no-service", "--api-port", str(p_api),
                         "--beacon-port", str(p_beacon), "--agent-port", str(pa1), "--rpc-port", str(ra1),
                         "--mem-gb", "4", "--bw-gbps", "100", "--limits-mode", "auto")
        code = next(l for l in r.stdout.splitlines() if l.startswith("DLLM1-"))
        self.assertIn("sha256 verificado", r.stdout)
        cfgA = json.loads((Path(envA["DLLM_HOME"]) / "config.json").read_text())
        self.assertEqual(cfgA["role"], "both")
        self.assertEqual(oct((Path(envA["DLLM_HOME"]) / "config.json").stat().st_mode & 0o777), "0o600")
        self.assertTrue((Path(envA["DLLM_HOME"]) / "dllm.pyz").exists())  # el programa se copió a un sitio estable
        self.assertEqual(cfgA["llama_tag"], "b9999")

        # --- equipo B: nodo, con SOLO el código de unión
        envB = self.env("B")
        r = self.run_pyz(envB, "setup", "--tui", "--yes", "--role", "node", "--join", code, "--backend", "cpu",
                         "--name", "equipoB", "--no-service", "--agent-port", str(pa2), "--rpc-port", str(ra2),
                         "--mem-gb", "8", "--bw-gbps", "20")
        cfgB = json.loads((Path(envB["DLLM_HOME"]) / "config.json").read_text())
        self.assertEqual(cfgB["secret"], cfgA["secret"])               # emparejados por el código
        self.assertEqual(cfgB["llama_tag"], "b9999")                   # misma build que el orquestador
        self.assertEqual(cfgB["orchestrator"]["beacon_port"], p_beacon)
        self.assertIn("127.0.0.1", cfgB["agent"]["allow_ip"])
        self.assertEqual((cfgB["agent"]["limits_mode"], cfgB["agent"]["mem_gb"]), ("manual", 8.0))  # fijado en el nodo
        self.assertEqual(len(cfgB["agent"]["allow_ip"]), 2)            # solo el orquestador (+ loopback)

        # --- arranque con `run` desde el .pyz instalado (lo mismo que ejecutaría el servicio)
        self.spawn(envA, str(Path(envA["DLLM_HOME"]) / "dllm.pyz"), "run")
        self.wait(lambda: self.http(p_api, "GET", "/healthz")[0] == 200, 30, "orquestador")
        self.spawn(envB, str(Path(envB["DLLM_HOME"]) / "dllm.pyz"), "run")
        self.wait(lambda: sum(n["up"] for n in self.http(p_api, "GET", "/api/state")[1]["nodes"]) == 2, 40,
                  "2 nodos (A local + B por código de unión)")

        # --- el panel se sirve desde dentro del .pyz
        st, html = self.http(p_api, "GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"DLLM", html)

        # --- cargar el modelo y usarlo a través de ambos nodos
        self.assertEqual(self.http(p_api, "POST", "/api/load", {"model": "mock.gguf", "ctx": 2048})[0], 202)
        self.wait(lambda: self.http(p_api, "GET", "/api/state")[1]["state"] in ("READY", "ERROR"), 60, "carga")
        s = self.http(p_api, "GET", "/api/state")[1]
        self.assertEqual(s["state"], "READY", s.get("error"))
        self.assertEqual(sorted(a["name"] for a in s["plan"]["assignments"]), ["equipoA", "equipoB"])
        st, r = self.http(p_api, "POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hola"}]})
        self.assertEqual((st, r["mock"]["echo_ok"]), (200, [True, True]))

        # --- diagnóstico local
        d = self.run_pyz(envA, "doctor", check=False)
        self.assertEqual(d.returncode, 0, d.stdout)
        self.assertIn("Programa en marcha", d.stdout)
        # ajuste desde el panel del nodo local (modo auto): se aplica y se guarda en su config.json
        st, r = self.http(p_api, "POST", "/api/node/limits", {"node": f"127.0.0.1:{pa1}", "share": 25})
        self.assertEqual(st, 200, r)
        saved = json.loads((Path(envA["DLLM_HOME"]) / "config.json").read_text())["agent"]
        self.assertEqual(saved["share"], 25.0)
        # el nodo B está en modo manual: el panel no puede cambiarlo
        st, r = self.http(p_api, "POST", "/api/node/limits", {"node": f"127.0.0.1:{pa2}", "share": 10})
        self.assertEqual(st, 400)
        # el código de unión también se puede volver a ver
        self.assertEqual(self.run_pyz(envA, "join-code").stdout.strip(), code)

    def test_2_errors_are_clear(self):
        env = self.env("C")
        r = self.run_pyz(env, "setup", "--tui", "--yes", "--role", "node", "--join", "DLLM1-corrupto-00000000",
                         "--backend", "cpu", "--no-service", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("código", r.stdout)
        self.assertFalse((Path(env["DLLM_HOME"]) / "config.json").exists())  # no deja nada a medias

        bad = self.make_release(self.tmp, "b9998", bad_digest=True)
        code = config.encode_join({"secret": "x", "orch": "127.0.0.1", "beacon": 50999})
        r = self.run_pyz(self.env("D", bad), "setup", "--tui", "--yes", "--role", "node", "--join", code,
                         "--backend", "cpu", "--no-service", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sha256", r.stdout)  # descarga manipulada → se rechaza
        self.assertFalse((Path(self.env("D", bad)["DLLM_HOME"]) / "config.json").exists())

    def test_3_web_wizard(self):
        env = self.env("W")
        p = subprocess.Popen([sys.executable, str(self.pyz), "setup", "--gui", "--no-browser"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        self.procs.append(p)
        url = p.stdout.readline().split("en: ", 1)[1].strip()
        port, token = int(url.split(":")[2].split("/")[0]), url.split("t=")[1]

        def req(method, path, body=None, host=None, tok=token, origin=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            h = {"Content-Type": "application/json"}
            if host:
                h["Host"] = host
            if origin:
                h["Origin"] = origin
            sep = "&" if "?" in path else "?"
            c.request(method, path + (f"{sep}t={tok}" if tok else ""), body=json.dumps(body) if body is not None else None, headers=h)
            r = c.getresponse()
            d = r.read()
            c.close()
            return r.status, d

        # protecciones: sin token, token malo, Host ajeno (DNS rebinding) y Origin ajeno (CSRF)
        self.assertEqual(req("GET", "/api/detect", tok=None)[0], 403)
        self.assertEqual(req("GET", "/api/detect", tok="malo")[0], 403)
        self.assertEqual(req("GET", "/api/detect", host="evil.example.com")[0], 403)
        self.assertEqual(req("POST", "/api/install", {"role": "node"}, origin="http://evil.example.com")[0], 403)
        st, d = req("GET", "/api/detect")
        self.assertEqual(st, 200)
        self.assertIn("suggested_backend", json.loads(d))
        st, html = req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(token.encode(), html)

        # validación en el formulario (400 con mensaje) y luego instalación real
        st, d = req("POST", "/api/install", {"role": "node", "join_code": "nope"})
        self.assertEqual(st, 400)
        self.assertIn("código", json.loads(d)["error"])
        code = config.encode_join({"secret": "web-secret", "orch": "127.0.0.1", "beacon": 50999, "tag": "b9999"})
        o = {"role": "node", "join_code": code, "backend": "cpu", "autostart": False, "name": "web-node",
             "agent_port": free_port(), "rpc_port": free_port()}
        self.assertEqual(req("POST", "/api/install", o)[0], 202)
        since, last = 0, None
        for _ in range(100):
            last = json.loads(req("GET", f"/api/progress?since={since}")[1])
            since += len(last["events"])
            if last["done"]:
                break
            time.sleep(0.3)
        self.assertTrue(last["done"])
        self.assertIsNone(last["error"], last)
        self.assertEqual(last["result"]["role"], "node")
        cfg = json.loads((Path(env["DLLM_HOME"]) / "config.json").read_text())
        self.assertEqual((cfg["role"], cfg["secret"], cfg["name"]), ("node", "web-secret", "web-node"))
        # explorador de carpetas del asistente (protegido por el token como el resto)
        self.assertEqual(req("GET", "/api/browse", tok=None)[0], 403)
        st, d = req("GET", "/api/browse?path=" + str(self.tmp).replace("/", "%2F"))
        self.assertEqual(st, 200)
        self.assertIn("dist", json.loads(d)["dirs"])
        st, d = req("POST", "/api/mkdir", {"path": str(self.tmp), "name": "modelos-web"})
        self.assertEqual(st, 200)
        self.assertTrue((self.tmp / "modelos-web").is_dir())
        self.assertEqual(req("POST", "/api/mkdir", {"path": str(self.tmp), "name": "../fuera"})[0], 400)
        self.assertEqual(req("POST", "/api/quit")[0], 200)
        p.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
