# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Slots de modelo: libro de memoria y handshake por slot."""
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dllm import auth, engine, hw, scheduler  # noqa: E402
from dllm.ledger import RAM_MARGIN, VRAM_MARGIN as SAFETY, Ledger  # noqa: E402

GiB = 1 << 30
SECRET = "secreto-de-prueba-secreto-de-prueba"


class TestLedger(unittest.TestCase):
    def test_available_subtracts_other_slots_and_safety(self):
        lg = Ledger()
        self.assertEqual(lg.available("n", 10 * GiB), 10 * GiB)  # sin reservas no se resta nada
        lg.reserve(0, {"n": 6 * GiB})
        self.assertEqual(lg.available("n", 10 * GiB), 4 * GiB - SAFETY)
        self.assertEqual(lg.available("n", 10 * GiB, exclude=0), 10 * GiB)  # un slot no se cuenta a sí mismo
        self.assertEqual(lg.available("otro", 10 * GiB), 10 * GiB)

    def test_release_and_floor(self):
        lg = Ledger()
        lg.reserve(1, {"n": 20 * GiB})
        self.assertEqual(lg.available("n", 10 * GiB), 0)
        lg.release(1)
        self.assertEqual(lg.available("n", 10 * GiB), 10 * GiB)

    def test_view(self):
        lg = Ledger()
        lg.reserve(0, {"a": 2 * GiB})
        lg.reserve(2, {"a": 1 * GiB, "b": 1 * GiB})
        v = lg.view({"a": 8 * GiB, "b": 4 * GiB})
        self.assertEqual(v["a"]["reserved"], {0: 2 * GiB, 2: 1 * GiB})
        self.assertEqual(v["a"]["free"], 5 * GiB - SAFETY)


class TestLedgerRam(unittest.TestCase):
    def test_ram_margin_and_live_cap(self):
        lg = Ledger(ram_margin=2 * GiB)
        self.assertEqual(lg.available_ram("n", 16 * GiB), 14 * GiB)  # el margen se resta siempre
        self.assertEqual(lg.available_ram("n", 16 * GiB, live_free=6 * GiB), 4 * GiB)  # tope por RAM libre real
        lg.reserve(0, ram={"n": 5 * GiB})
        self.assertEqual(lg.available_ram("n", 16 * GiB), 9 * GiB)
        self.assertEqual(lg.available_ram("n", 16 * GiB, exclude=0), 14 * GiB)

    def test_exclude_many_slots(self):
        lg = Ledger()
        lg.reserve(1, vram={"n": 4 * GiB})
        lg.reserve(2, vram={"n": 3 * GiB})
        self.assertEqual(lg.reserved("n", "vram", exclude={1, 2}), 0)
        self.assertEqual(lg.reserved("n", "vram", exclude={1}), 3 * GiB)

    def test_default_margins(self):
        self.assertEqual(Ledger().ram_margin, RAM_MARGIN)
        self.assertEqual(Ledger().vram_margin, SAFETY)


class TestSlotHandshake(unittest.TestCase):
    def run_hs(self, slot, probe=False, server_secret=SECRET):
        a, b = socket.socketpair()
        a.settimeout(5)
        b.settimeout(5)
        out = {}

        def srv():
            try:
                out["mode"] = auth.server_handshake(b, server_secret)
            except Exception as e:
                out["err"] = e
        t = threading.Thread(target=srv)
        t.start()
        try:
            auth.client_handshake(a, SECRET, probe=probe, slot=slot)
        except Exception:
            pass
        t.join(5)
        a.close()
        b.close()
        return out

    def test_slot_is_carried_in_handshake(self):
        self.assertEqual(self.run_hs(0)["mode"], "rpc")  # compatible con 0.4.x
        self.assertEqual(self.run_hs(3)["mode"], "rpc:3")
        self.assertEqual(self.run_hs(auth.MAX_SLOTS - 1)["mode"], f"rpc:{auth.MAX_SLOTS - 1}")
        self.assertEqual(self.run_hs(0, probe=True)["mode"], "probe")

    def test_wrong_secret_or_out_of_range_slot_rejected(self):
        self.assertIn("err", self.run_hs(2, server_secret="otro-secreto-otro-secreto-otro"))
        self.assertIn("err", self.run_hs(auth.MAX_SLOTS + 2))


class TestDevicePolicy(unittest.TestCase):
    M = {"name": "m", "size": 8 * GiB, "n_layers": 32, "n_embd": 4096, "n_head": 32, "n_head_kv": 8}

    def test_offload_layers_is_monotonic_and_bounded(self):
        self.assertEqual(scheduler.offload_layers(self.M, 2048, 0), 0)
        self.assertEqual(scheduler.offload_layers(self.M, 2048, 100 * GiB), 32)
        a = scheduler.offload_layers(self.M, 2048, 4 * GiB)
        b = scheduler.offload_layers(self.M, 2048, 6 * GiB)
        self.assertTrue(0 < a < b < 32)
        self.assertLessEqual(scheduler.need_bytes(scheduler.sub_model(self.M, a), 2048), 4 * GiB)
        self.assertGreater(scheduler.need_bytes(scheduler.sub_model(self.M, a + 1), 2048), 4 * GiB)

    def test_host_plan_ok_and_rejected(self):
        ok = scheduler.host_plan(self.M, 2048, 20 * GiB)
        self.assertTrue(ok["ok"] and ok["ngl"] == 0 and ok["assignments"] == [])
        no = scheduler.host_plan(self.M, 2048, 2 * GiB, label="cpu_only")
        self.assertFalse(no["ok"])
        self.assertIn("RAM", no["reason"])

    def test_engine_estimate_memory_by_policy(self):
        gpu = engine.driver_for({}).estimate_memory(self.M, 2048, {})
        cpu = engine.driver_for({}).estimate_memory(self.M, 2048, {"device_policy": "cpu_only"})
        self.assertEqual(cpu["vram"], 0)
        self.assertEqual(cpu["ram"], gpu["vram"])
        self.assertEqual(gpu["ram"], scheduler.IO_RAM)

    def test_slot_ports_start_at_18081_and_skip_taken(self):
        p = hw.slot_port()
        self.assertGreaterEqual(p, hw.SLOT_PORTS[0])
        self.assertNotEqual(hw.slot_port({p}), p)


if __name__ == "__main__":
    unittest.main()
