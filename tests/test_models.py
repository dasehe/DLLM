# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Gestor de modelos contra un Hugging Face / Ollama simulados: búsqueda, descargas verificadas, troceados,
reanudación, cancelación, espacio en disco, proyectores multimodales, importación de Ollama local y parámetros."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from dllm import models  # noqa: E402
from mock_hub import Hub, gguf_bytes  # noqa: E402

REPO = "acme/Tiny-GGUF"
TOKEN = "hf_" + "a" * 30


def wait_job(mgr, jid, timeout=20, until=("completada", "error", "cancelada")):
    end = time.time() + timeout
    while time.time() < end:
        j = next(x for x in mgr.views() if x["id"] == jid)
        if j["status"] in until:
            return j
        time.sleep(0.05)
    raise AssertionError(f"la descarga {jid} no terminó: {j}")


class TestModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = Hub()
        h = cls.hub
        cls.q4 = gguf_bytes(300_000, name="tiny")
        h.add_hf(REPO, "tiny-Q4_K_M.gguf", cls.q4)
        h.add_hf(REPO, "tiny-Q8_0.gguf", gguf_bytes(500_000))
        h.add_hf(REPO, "mmproj-tiny-BF16.gguf", gguf_bytes(50_000, arch="clip"))
        h.add_hf(REPO, "mmproj-tiny-F16.gguf", gguf_bytes(60_000, arch="clip"))
        h.add_hf(REPO, "README.md", b"# hola")
        h.add_hf("acme/Big-GGUF", "big-Q4_K_M-00001-of-00002.gguf", gguf_bytes(200_000))
        h.add_hf("acme/Big-GGUF", "big-Q4_K_M-00002-of-00002.gguf", gguf_bytes(150_000))
        h.add_hf("acme/Bad-GGUF", "badsha-Q4_0.gguf", gguf_bytes(100_000), lfs={"oid": "0" * 64, "size": 100_000})
        h.add_hf("acme/Bad-GGUF", "notgguf-Q4_0.gguf", b"NOPE" * 25_000)
        h.add_hf("acme/Slow-GGUF", "slow-Q4_0.gguf", gguf_bytes(3_000_000))
        h.slow.add("slow-Q4_0.gguf")
        cls.ollama_model = gguf_bytes(250_000, arch="gemma3", name="gemma")
        h.add_ollama("gemma", "e2b", [
            (models.OLLAMA_MODEL, cls.ollama_model),
            (models.OLLAMA_PROJECTOR, gguf_bytes(40_000, arch="clip")),
            (models.OLLAMA_PARAMS, json.dumps({"temperature": 1.0, "top_k": 64, "stop": ["<end_of_turn>"]}).encode()),
            (models.OLLAMA_SYSTEM, "Eres un asistente útil.".encode()),
        ])
        os.environ["DLLM_HF_URL"] = h.url
        os.environ["DLLM_OLLAMA_URL"] = h.url

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()
        os.environ.pop("DLLM_HF_URL", None)
        os.environ.pop("DLLM_OLLAMA_URL", None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "models"
        self.mgr = models.Manager(self.root, self.tmp / "models.json", self.tmp / "secrets.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ Hugging Face
    def test_search_and_files(self):
        self.assertEqual([r["id"] for r in models.hf_search("tiny")], [REPO])
        files = {f["file"]: f for f in models.hf_files(REPO)}
        self.assertEqual(files["tiny-Q4_K_M.gguf"]["quant"], "Q4_K_M")
        self.assertTrue(files["mmproj-tiny-F16.gguf"]["mmproj"])
        self.assertNotIn("README.md", files)
        big = models.hf_files("acme/Big-GGUF")
        self.assertEqual((len(big), big[0]["parts"], big[0]["size"]), (1, 2, 350_000))
        with self.assertRaises(models.ModelError):
            models.hf_search("")

    def test_download_with_projector_and_token_not_leaked(self):
        self.mgr.set_hf_token(TOKEN)
        self.hub.auth_seen = {"api": [], "cdn": []}
        j = wait_job(self.mgr, self.mgr.add_hf(REPO, "tiny-Q4_K_M.gguf")["id"])
        self.assertEqual(j["status"], "completada", j)
        d = self.root / "hf" / "acme__Tiny-GGUF"
        self.assertEqual((d / "tiny-Q4_K_M.gguf").read_bytes(), self.q4)
        self.assertTrue((d / "mmproj-tiny-F16.gguf").exists())          # proyector preferido: F16
        self.assertFalse((d / "mmproj-tiny-BF16.gguf").exists())
        lst = self.mgr.list()
        self.assertEqual([m["name"] for m in lst], ["hf/acme__Tiny-GGUF/tiny-Q4_K_M.gguf"])  # el mmproj no es un modelo
        self.assertEqual(lst[0]["mmproj"], "hf/acme__Tiny-GGUF/mmproj-tiny-F16.gguf")
        self.assertEqual(lst[0]["arch"], "llama")
        # el token va a la API de HF pero NO a la CDN (otro host tras la redirección)
        self.assertIn(f"Bearer {TOKEN}", self.hub.auth_seen["api"])
        self.assertTrue(self.hub.auth_seen["cdn"])
        self.assertTrue(all(a is None for a in self.hub.auth_seen["cdn"]), self.hub.auth_seen["cdn"])

    def test_split_model(self):
        j = wait_job(self.mgr, self.mgr.add_hf("acme/Big-GGUF", "big-Q4_K_M-00001-of-00002.gguf")["id"])
        self.assertEqual(j["status"], "completada", j)
        lst = self.mgr.list()
        self.assertEqual(len(lst), 1)
        self.assertEqual((lst[0]["parts"], lst[0]["size"]), (2, 350_000))
        self.mgr.delete(lst[0]["name"])
        self.assertEqual(list((self.root / "hf").rglob("*.gguf")), [])  # borra todas las partes

    def test_bad_sha_and_not_gguf_are_rejected(self):
        for f, frag in (("badsha-Q4_0.gguf", "sha256"), ("notgguf-Q4_0.gguf", "GGUF")):
            j = wait_job(self.mgr, self.mgr.add_hf("acme/Bad-GGUF", f)["id"])
            self.assertEqual(j["status"], "error")
            self.assertIn(frag, j["error"])
        self.assertEqual(list(self.root.rglob("*")), [p for p in self.root.rglob("*") if p.is_dir()])  # nada a medias

    def test_resume(self):
        d = self.root / "hf" / "acme__Tiny-GGUF"
        d.mkdir(parents=True)
        (d / "tiny-Q8_0.gguf.part").write_bytes(self.hub.files[(REPO, "tiny-Q8_0.gguf")][:200_000])
        self.hub.served.pop("tiny-Q8_0.gguf", None)
        j = wait_job(self.mgr, self.mgr.add_hf(REPO, "tiny-Q8_0.gguf")["id"])
        self.assertEqual(j["status"], "completada", j)
        self.assertEqual((d / "tiny-Q8_0.gguf").read_bytes(), self.hub.files[(REPO, "tiny-Q8_0.gguf")])
        self.assertEqual(self.hub.served["tiny-Q8_0.gguf"], 300_000)  # solo lo que faltaba

    def test_cancel_keeps_part_for_resume(self):
        jid = self.mgr.add_hf("acme/Slow-GGUF", "slow-Q4_0.gguf")["id"]
        wait_job(self.mgr, jid, until=("descargando",))
        time.sleep(0.3)
        self.mgr.cancel(jid)
        j = wait_job(self.mgr, jid)
        self.assertEqual(j["status"], "cancelada")
        self.assertTrue(list(self.root.rglob("slow-Q4_0.gguf.part")))
        self.assertFalse(list(self.root.rglob("slow-Q4_0.gguf")))

    def test_clear_download_history(self):
        a = self.mgr.add_hf("acme/Slow-GGUF", "slow-Q4_0.gguf")["id"]
        wait_job(self.mgr, a, until=("descargando",))
        with self.assertRaises(models.ModelError):
            self.mgr.clear(a)                      # activa: no se puede borrar
        self.mgr.cancel(a)
        wait_job(self.mgr, a)
        with self.assertRaises(models.ModelError):
            self.mgr.clear("no-existe")
        self.assertEqual(self.mgr.clear(a), 1)     # terminada: se quita del historial
        self.assertEqual(self.mgr.views(), [])
        self.assertTrue(list(self.root.rglob("slow-Q4_0.gguf.part")))   # el .part (reanudar) se conserva

    def test_clear_all_keeps_active(self):
        a = self.mgr.add_hf("acme/Slow-GGUF", "slow-Q4_0.gguf")["id"]
        wait_job(self.mgr, a, until=("descargando",))
        self.mgr.jobs.append(models.Job("vieja", "hf", [], self.root))
        self.mgr.jobs[-1].status = "error"
        self.mgr.jobs.append(models.Job("hecha", "hf", [], self.root))
        self.mgr.jobs[-1].status = "completada"
        self.assertEqual(self.mgr.clear(), 2)
        self.assertEqual([v["id"] for v in self.mgr.views()], [a])
        self.mgr.cancel(a)
        wait_job(self.mgr, a)

    def test_disk_space_checked(self):
        DU = namedtuple("DU", "total used free")
        with mock.patch.object(models.shutil, "disk_usage", return_value=DU(10, 10, 1000)):
            j = wait_job(self.mgr, self.mgr.add_hf(REPO, "tiny-Q4_K_M.gguf")["id"])
        self.assertEqual(j["status"], "error")
        self.assertIn("espacio", j["error"])

    # ------------------------------------------------------------ Ollama
    def test_ollama_pull_with_projector_and_params(self):
        j = wait_job(self.mgr, self.mgr.add_ollama("gemma:e2b")["id"])
        self.assertEqual(j["status"], "completada", j)
        self.assertEqual((self.root / "ollama" / "gemma-e2b.gguf").read_bytes(), self.ollama_model)
        m = self.mgr.list()[0]
        self.assertEqual(m["name"], "ollama/gemma-e2b.gguf")
        self.assertEqual(m["mmproj"], "ollama/gemma-e2b-mmproj.gguf")
        s = m["settings"]
        self.assertEqual((s["temperature"], s["top_k"], s["stop"]), (1.0, 64, ["<end_of_turn>"]))
        self.assertEqual(s["system_prompt"], "Eres un asistente útil.")
        with self.assertRaises(models.ModelError):
            self.mgr.add_ollama("no-existe:1b")

    def test_ollama_local_import(self):
        ol = self.tmp / "ollama-home"
        blobs, man_dir = ol / "blobs", ol / "manifests" / "registry.ollama.ai" / "library" / "qwen2.5"
        blobs.mkdir(parents=True)
        man_dir.mkdir(parents=True)
        data = gguf_bytes(120_000)
        dg = hashlib.sha256(data).hexdigest()
        (blobs / f"sha256-{dg}").write_bytes(data)
        params = json.dumps({"temperature": 0.6}).encode()
        pg = hashlib.sha256(params).hexdigest()
        (blobs / f"sha256-{pg}").write_bytes(params)
        (man_dir / "7b").write_text(json.dumps({"layers": [
            {"mediaType": models.OLLAMA_MODEL, "digest": f"sha256:{dg}", "size": len(data)},
            {"mediaType": models.OLLAMA_PARAMS, "digest": f"sha256:{pg}", "size": len(params)}]}))
        with mock.patch.dict(os.environ, {"OLLAMA_MODELS": str(ol)}):
            self.assertEqual([m["name"] for m in models.ollama_local_list()], ["qwen2.5:7b"])
            j = wait_job(self.mgr, self.mgr.add_ollama_local("qwen2.5:7b")["id"])
        self.assertEqual(j["status"], "completada", j)
        m = self.mgr.list()[0]
        self.assertEqual((m["name"], m["settings"]["temperature"]), ("ollama/qwen2.5-7b.gguf", 0.6))

    # ------------------------------------------------------------ validación y parámetros
    def test_names_are_validated(self):
        for bad in ("../x", "a/b/c", "", "x"):
            with self.assertRaises(models.ModelError):
                self.mgr.add_hf(bad, "f.gguf")
        for f in ("../../etc/passwd.gguf", "/abs.gguf", "x.bin"):
            with self.assertRaises(models.ModelError):
                self.mgr.add_hf(REPO, f)
        for bad in ("BAD NAME", "../x", "a:b:c"):
            with self.assertRaises(models.ModelError):
                models.parse_ollama(bad)
        self.assertEqual(models.parse_ollama("qwen2.5:7b"), ("library", "qwen2.5", "7b"))
        self.assertEqual(models.parse_ollama("user/m"), ("user", "m", "latest"))
        with self.assertRaises(models.ModelError):
            self.mgr.delete("../../etc/passwd.gguf")

    def test_settings_validation_and_injection(self):
        self.assertEqual(models.validate_settings({"temperature": "0.7", "top_k": 40, "stop": "a, b"}),
                         {"temperature": 0.7, "top_k": 40, "stop": ["a", "b"]})
        for bad in ({"temperature": 9}, {"top_k": 1.5}, {"top_p": "x"}, {"unknown": 1}, {"mmproj": "../x.gguf"}):
            with self.assertRaises(models.ModelError):
                models.validate_settings(bad)
        s = {"temperature": 0.2, "max_tokens": 64, "system_prompt": "Sé breve"}
        b = models.apply_settings("/v1/chat/completions", {"messages": [{"role": "user", "content": "hola"}]}, s)
        self.assertEqual((b["temperature"], b["max_tokens"], b["messages"][0]["role"]), (0.2, 64, "system"))
        b = models.apply_settings("/v1/chat/completions", {"temperature": 1.0, "messages": [
            {"role": "system", "content": "mío"}, {"role": "user", "content": "x"}]}, s)
        self.assertEqual((b["temperature"], len(b["messages"])), (1.0, 2))
        self.assertTrue(b["messages"][0]["content"].startswith("mío\n\nFecha y hora actuales"))

    def test_server_datetime(self):
        import time as _t
        t = _t.struct_time((2026, 11, 15, 10, 32, 0, 6, 319, 0))
        self.assertTrue(models.now_text("datetime", t).startswith(
            "Fecha y hora actuales (reloj del servidor): domingo, 15 de noviembre de 2026, 10:32 ("))
        self.assertEqual(models.now_text("date", t), "Fecha actual (reloj del servidor): domingo, 15 de noviembre de 2026.")
        msg = lambda: {"messages": [{"role": "user", "content": "¿qué día es?"}]}
        b = models.apply_settings("/v1/chat/completions", msg(), None, now=t)  # sin ajustes: activado por defecto
        self.assertIn("15 de noviembre de 2026", b["messages"][0]["content"])
        b = models.apply_settings("/v1/chat/completions", msg(), {"datetime": "off"}, now=t)
        self.assertEqual(b["messages"][0]["role"], "user")
        b = models.apply_settings("/v1/chat/completions", msg(), {"system_prompt": "Sé breve", "datetime": "date"}, now=t)
        self.assertEqual(b["messages"][0]["content"], "Sé breve\n\nFecha actual (reloj del servidor): domingo, "
                                                      "15 de noviembre de 2026.")
        b = models.apply_settings("/v1/completions", {"prompt": "x"}, None, now=t)
        self.assertEqual(b, {"prompt": "x"})  # completions sin chat: no se toca
        self.assertEqual(models.validate_settings({"datetime": "date"}), {"datetime": "date"})
        with self.assertRaises(models.ModelError):
            models.validate_settings({"datetime": "ayer"})

    def test_mmproj_autodetect_and_disable(self):
        d = self.root / "local"
        d.mkdir(parents=True)
        (d / "vis-Q4_K_M.gguf").write_bytes(gguf_bytes(10_000))
        (d / "mmproj-vis-Q8_0.gguf").write_bytes(gguf_bytes(5_000))
        (d / "mmproj-vis-f16.gguf").write_bytes(gguf_bytes(6_000))
        self.assertTrue(self.mgr.mmproj_for("local/vis-Q4_K_M.gguf").endswith("mmproj-vis-f16.gguf"))
        self.mgr.set_settings("local/vis-Q4_K_M.gguf", {"mmproj": ""})   # desactivado a propósito
        self.assertIsNone(self.mgr.mmproj_for("local/vis-Q4_K_M.gguf"))
        self.mgr.set_settings("local/vis-Q4_K_M.gguf", {"mmproj": "local/mmproj-vis-Q8_0.gguf"})
        self.assertTrue(self.mgr.mmproj_for("local/vis-Q4_K_M.gguf").endswith("mmproj-vis-Q8_0.gguf"))

    @unittest.skipIf(os.name == "nt", "enlaces simbólicos")
    def test_models_dir_through_symlink(self):
        """La carpeta de modelos puede ser un enlace simbólico (en macOS /var lo es)."""
        real = self.tmp / "real"
        (real / "local").mkdir(parents=True)
        (real / "local" / "vis-Q4_K_M.gguf").write_bytes(gguf_bytes(10_000))
        (real / "local" / "mmproj-vis-f16.gguf").write_bytes(gguf_bytes(5_000))
        link = self.tmp / "enlace"
        link.symlink_to(real, target_is_directory=True)
        mgr = models.Manager(link, self.tmp / "m2.json", self.tmp / "s2.json")
        lst = mgr.list()
        self.assertEqual([(m["name"], m["mmproj"]) for m in lst], [("local/vis-Q4_K_M.gguf", "local/mmproj-vis-f16.gguf")])
        self.assertTrue(mgr.mmproj_for("local/vis-Q4_K_M.gguf").endswith("mmproj-vis-f16.gguf"))

    def test_hf_token_format_and_storage(self):
        with self.assertRaises(models.ModelError):
            self.mgr.set_hf_token("no-es-un-token")
        self.mgr.set_hf_token(TOKEN)
        self.assertEqual(self.mgr.hf_token(), TOKEN)
        if os.name != "nt":
            self.assertEqual(oct((self.tmp / "secrets.json").stat().st_mode & 0o777), "0o600")
        self.mgr.set_hf_token("")
        self.assertIsNone(self.mgr.hf_token())


if __name__ == "__main__":
    unittest.main(verbosity=2)
