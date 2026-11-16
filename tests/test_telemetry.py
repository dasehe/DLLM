# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Telemetría por fabricante (con salidas en el formato real de cada fuente), métricas de llama-server,
capas fijas por nodo y caché KV cuantizada."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dllm import models, scheduler, telemetry  # noqa: E402
from dllm.orchestrator import parse_prometheus  # noqa: E402

GiB, MiB = 1 << 30, 1 << 20


class TestParsers(unittest.TestCase):
    def test_nvidia_smi(self):
        out = ("0, NVIDIA GeForce RTX 3060, 87, 9876, 12288, 66, 151.23, 170.00, 54, 1852\n"
               "1, NVIDIA GeForce GT 1030, 0, 5, 2048, 35, [N/A], [N/A], [Not Supported], 139\n")
        g = telemetry.parse_nvidia_smi(out)
        self.assertEqual(len(g), 2)
        self.assertEqual((g[0]["util"], g[0]["vram_used"], g[0]["vram_total"]), (87.0, 9876 * MiB, 12288 * MiB))
        self.assertEqual((g[0]["temp"], g[0]["power"], g[0]["power_limit"], g[0]["fan"]), (66.0, 151.23, 170.0, 54.0))
        self.assertIsNone(g[1]["power"])  # [N/A] → no disponible
        self.assertIsNone(g[1]["fan"])

    def test_amdgpu_sysfs(self):
        root = Path(tempfile.mkdtemp())
        try:
            d = root / "card1" / "device"
            (d / "hwmon" / "hwmon3").mkdir(parents=True)
            (root / "card1-DP-1").mkdir()  # conector: se ignora
            files = {"vendor": "0x1002", "mem_info_vram_total": str(16 * GiB), "mem_info_vram_used": str(12 * GiB),
                     "gpu_busy_percent": "71", "hwmon/hwmon3/temp1_input": "63000",
                     "hwmon/hwmon3/power1_input": "131000000", "hwmon/hwmon3/power1_cap": "190000000",
                     "hwmon/hwmon3/fan1_input": "1650"}
            for k, v in files.items():
                (d / k).write_text(v + "\n")
            intel = root / "card0" / "device"
            intel.mkdir(parents=True)
            (intel / "vendor").write_text("0x8086\n")  # Intel: no es amdgpu
            g = telemetry.amdgpu_sysfs(root)
            self.assertEqual(len(g), 1)
            g = g[0]
            self.assertEqual((g["util"], g["vram_used"], g["vram_total"]), (71.0, 12 * GiB, 16 * GiB))
            self.assertEqual((g["temp"], g["power"], g["power_limit"], g["fan"]), (63.0, 131.0, 190.0, 1650.0))
        finally:
            shutil.rmtree(root)

    def test_windows_counters_like_task_manager(self):
        luid_d, luid_i = "luid_0x00000000_0x0000C2A3", "luid_0x00000000_0x0000BEEF"
        eng = {f"pid_100_{luid_d}_phys_0_eng_0_engtype_3D": 30.0,
               f"pid_200_{luid_d}_phys_0_eng_0_engtype_3D": 25.0,      # mismo motor: se suman procesos
               f"pid_300_{luid_d}_phys_0_eng_2_engtype_Compute": 70.0,  # el motor más ocupado manda
               f"pid_100_{luid_i}_phys_0_eng_0_engtype_3D": 5.0}
        mem = {f"{luid_d}_phys_0": 9.5 * GiB, f"{luid_i}_phys_0": 128 * MiB}
        r = telemetry.aggregate_wddm(eng, mem)
        self.assertEqual(r[luid_d.lower()]["util"], 70.0)
        self.assertEqual(r[luid_d.lower()]["vram_used"], int(9.5 * GiB))
        self.assertEqual(r[luid_i.lower()]["util"], 5.0)

    def test_ioreg(self):
        out = '''+-o AGXAcceleratorG14X  <class AGXAcceleratorG14X>
    "PerformanceStatistics" = {"In use system memory"=6442450944,"Device Utilization %"=37,"Renderer Utilization %"=35}'''
        self.assertEqual(telemetry.parse_ioreg(out), {"util": 37.0, "vram_used": 6442450944})
        self.assertIsNone(telemetry.parse_ioreg("nada"))

    def test_prometheus(self):
        text = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 1234
llamacpp:tokens_predicted_total 5678
llamacpp:requests_processing 2
llamacpp:kv_cache_usage_ratio 0.25
"""
        m = parse_prometheus(text)
        self.assertEqual((m["llamacpp:tokens_predicted_total"], m["llamacpp:requests_processing"]), (5678.0, 2.0))

    def test_sampler_matches_device_and_measures_rpc_traffic(self):
        root = Path(tempfile.mkdtemp())
        try:
            d = root / "card0" / "device"
            d.mkdir(parents=True)
            for k, v in {"vendor": "0x1002", "mem_info_vram_total": str(16 * GiB), "mem_info_vram_used": str(GiB),
                         "gpu_busy_percent": "12"}.items():
                (d / k).write_text(v)
            dev = {"name": "Vulkan0", "desc": "AMD Radeon RX 7600 XT", "total": 16 * GiB, "free": 15 * GiB}
            with mock.patch.dict(os.environ, {"DLLM_SYSFS_DRM": str(root), "PATH": ""}):
                s = telemetry.Sampler(lambda: dev, interval=0.1)
                s.sample()
                s.add_bytes("rx", 3_000_000)
                s.add_bytes("tx", 1_000_000)
                time.sleep(0.2)
                r = s.sample()
            if sys.platform.startswith("linux"):
                self.assertEqual((r["gpu"]["source"], r["gpu"]["util"]), ("amdgpu", 12.0))
                self.assertEqual(r["gpu"]["name"], "AMD Radeon RX 7600 XT")  # el nombre de llama.cpp
            self.assertGreater(r["rpc_rx_bps"], 0)
            self.assertGreater(r["rpc_rx_bps"], r["rpc_tx_bps"])
            self.assertIsNotNone(r["ram_total"])
            self.assertIsNone(telemetry.Sampler(lambda: None).sample()["gpu"])  # nodo solo CPU
        finally:
            shutil.rmtree(root)


class TestSchedulerExtras(unittest.TestCase):
    m = {"name": "m", "size": 10 * GiB, "n_layers": 40, "n_embd": 4096, "n_head": 32, "n_head_kv": 8,
         "ctx_train": 131072}

    def node(self, i, cap, bw, layers=None):
        return {"id": i, "name": i, "backend": "gpu", "cap_bytes": cap * GiB, "bw_gbps": bw, "mbps": 940,
                "rtt_ms": 0.3, "layers": layers}

    def test_fixed_layers(self):
        p = scheduler.plan(self.m, [self.node("a", 12, 360), self.node("b", 16, 288, layers=30)])
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual([(a["name"], a["layers"], a["fixed"]) for a in p["assignments"]],
                         [("a", 10, False), ("b", 30, True)])
        self.assertAlmostEqual(sum(p["tensor_split"]), 1.0, places=3)

    def test_fixed_layers_errors(self):
        p = scheduler.plan(self.m, [self.node("a", 12, 360, layers=30), self.node("b", 16, 288, layers=20)])
        self.assertIn("superan", p["reason"])
        p = scheduler.plan(self.m, [self.node("a", 4, 360, layers=39), self.node("b", 16, 288)])
        self.assertIn("baja sus capas fijas", p["reason"])

    def test_quantized_kv_cache_needs_less_memory(self):
        f16 = scheduler.kv_bytes(self.m, 32768)
        q8 = scheduler.kv_bytes(self.m, 32768, "q8_0", "q8_0")
        self.assertAlmostEqual(q8 / f16, 34 / 64, places=3)
        nodes = [self.node("a", 16, 300)]
        self.assertGreater(scheduler.plan(self.m, nodes, 4096, k_type="q4_0", v_type="q4_0")["max_ctx"],
                           scheduler.plan(self.m, nodes, 4096)["max_ctx"])

    def test_load_settings(self):
        s = models.validate_settings({"cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attn": "on",
                                      "batch_size": 1024, "ubatch_size": 256, "parallel": 2, "seed": 7,
                                      "presence_penalty": 0.3, "rope_scaling": "yarn"})
        self.assertEqual(models.load_args(s), ["-ctk", "q8_0", "-ctv", "q8_0", "-fa", "on", "-b", "1024", "-ub", "256",
                                               "-np", "2", "--rope-scaling", "yarn"])
        for bad in ({"cache_type_k": "q3"}, {"flash_attn": "si"}, {"cache_type_v": "q4_0", "flash_attn": "off"},
                    {"batch_size": 256, "ubatch_size": 512}, {"parallel": 0}, {"seed": -5}):
            with self.assertRaises(models.ModelError):
                models.validate_settings(bad)
        b = models.apply_settings("/v1/chat/completions", {"messages": []}, {"seed": 7, "presence_penalty": 0.3,
                                                                              "ctx": 8192, "cache_type_k": "q8_0"})
        self.assertEqual((b["seed"], b["presence_penalty"]), (7, 0.3))
        self.assertNotIn("ctx", b)            # los ajustes de carga no van en la petición
        self.assertNotIn("cache_type_k", b)


@unittest.skipIf(os.name == "nt", "nvidia-smi simulado con shebang")
class TestNvidiaSmiFake(unittest.TestCase):
    def test_sampler_uses_nvidia_smi(self):
        d = Path(tempfile.mkdtemp())
        try:
            exe = d / "nvidia-smi"
            exe.write_text("#!/bin/sh\necho '0, NVIDIA GeForce RTX 3060, 64, 8000, 12288, 61, 120.5, 170.00, 45, 1800'\n")
            exe.chmod(0o755)
            dev = {"name": "CUDA0", "desc": "NVIDIA GeForce RTX 3060", "total": 12288 * MiB, "free": 4000 * MiB}
            with mock.patch.dict(os.environ, {"PATH": str(d)}):
                g = telemetry.Sampler(lambda: dev).sample()["gpu"]
            self.assertEqual((g["source"], g["util"], g["power"], g["temp"]), ("nvidia-smi", 64.0, 120.5, 61.0))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
