# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Reparto consciente de la topología de red: tiers, saltos mínimos y sonda del túnel."""
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dllm import auth, hw, scheduler, util  # noqa: E402

GiB = 1 << 30
MODEL = {"name": "m", "size": 6 * GiB, "n_layers": 32, "n_embd": 4096, "n_head": 32, "n_head_kv": 8}


def nd(i, cap, bw, rtt=0.4, mbps=940, local=False):
    return {"id": i, "name": i, "backend": "cuda", "cap_bytes": int(cap * GiB), "bw_gbps": bw, "mbps": mbps,
            "rtt_ms": rtt, "local": local}


class TestClassify(unittest.TestCase):
    def test_tiers(self):
        c = scheduler.classify
        self.assertEqual(c(True, 45, 100), 0)
        self.assertEqual(c(False, 0.3, 2500), 1)
        self.assertEqual(c(False, 0.3, 940), 2)    # 1 GbE
        self.assertEqual(c(False, 2.0, 2500), 2)   # RTT ≥ 1 ms: ya no es «rápida»
        self.assertEqual(c(False, 6.0, 940), 2)    # hueco 3-10 ms → tier 2
        self.assertEqual(c(False, 45, 940), 3)
        self.assertEqual(c(False, None, None), 2)


class TestTopologyPlan(unittest.TestCase):
    def test_lower_tier_filled_first_even_if_slower(self):
        # a (tier 3) es el más rápido pero está lejos: se usan b (tier 0) y c (tier 2)
        p = scheduler.plan(MODEL, [nd("a", 4.5, 200, rtt=45), nd("b", 4.5, 20, local=True, rtt=0.1),
                                   nd("c", 4.5, 50, rtt=0.5)])
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual({a["node_id"] for a in p["assignments"]}, {"b", "c"})
        self.assertEqual(p["max_tier"], 2)
        self.assertEqual([u["node"] for u in p["unused"]], ["a"])
        self.assertFalse(any("tier 3" in w for w in p["warnings"]))

    def test_tier3_only_when_needed_and_warns(self):
        p = scheduler.plan(MODEL, [nd("loc", 4.5, 50, local=True, rtt=0.1), nd("far", 4.5, 100, rtt=45)])
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual(p["max_tier"], 3)
        self.assertTrue(any("tier 3" in w for w in p["warnings"]))
        self.assertEqual([a["tier"] for a in p["assignments"]], [0, 3])  # el remoto va último y con menos carga

    def test_fewer_cuts_beats_more_nodes(self):
        # un solo nodo remoto grande en vez de tres pequeños del mismo tier
        p = scheduler.plan(MODEL, [nd("big", 8, 30), nd("s1", 3, 100), nd("s2", 3, 100), nd("s3", 3, 100)])
        self.assertEqual([a["node_id"] for a in p["assignments"]], ["big"])

    def test_local_gpus_do_not_count_as_cuts(self):
        p = scheduler.plan(MODEL, [nd("g1", 4.5, 100, local=True, rtt=0.1), nd("g2", 4.5, 100, local=True, rtt=0.1),
                                   nd("lan", 8, 100)])
        self.assertEqual({a["node_id"] for a in p["assignments"]}, {"g1", "g2"})  # 0 saltos de red
        self.assertEqual(p["max_tier"], 0)

    def test_does_not_fit_message_unchanged(self):
        p = scheduler.plan(MODEL, [nd("a", 2, 100), nd("b", 2, 50, rtt=45)])
        self.assertFalse(p["ok"])
        self.assertIn("no cabe", p["reason"])

    def test_fixed_layers_still_respected(self):
        a = nd("a", 8, 100)
        a["layers"] = 10
        p = scheduler.plan(MODEL, [a, nd("b", 8, 50)])
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual({x["node_id"]: x["layers"] for x in p["assignments"]}["a"], 10)

    def test_many_nodes_greedy_fallback(self):
        nodes = [nd(f"n{i}", 1.2, 50 + i) for i in range(scheduler.MAX_SUBSET_NODES + 3)]
        p = scheduler.plan(MODEL, nodes)
        self.assertTrue(p["ok"], p["reason"])


class TestProbe(unittest.TestCase):
    def serve(self, secret):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)

        def run():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                def one(c=c):
                    try:
                        c.settimeout(5)
                        if auth.server_handshake(c, secret) == "probe":
                            auth.serve_probe(c)
                    except Exception:
                        pass
                    finally:
                        c.close()
                threading.Thread(target=one, daemon=True).start()
        threading.Thread(target=run, daemon=True).start()
        return srv, srv.getsockname()[1]

    def test_probe_measures_link(self):
        srv, port = self.serve("s3cret-s3cret-s3cret")
        try:
            r = util.probe_link("127.0.0.1", port, "s3cret-s3cret-s3cret", size=1 << 20)
            self.assertEqual(r["via"], "tunel")
            self.assertGreater(r["mbps"], 10)
            self.assertLess(r["rtt_ms"], 200)
        finally:
            srv.close()

    def test_probe_rejects_wrong_secret(self):
        srv, port = self.serve("s3cret-s3cret-s3cret")
        try:
            with self.assertRaises(Exception):
                util.probe_link("127.0.0.1", port, "otro-secreto-otro-secreto", size=1 << 16)
        finally:
            srv.close()

    def test_probe_size_is_bounded(self):
        a, b = socket.socketpair()
        a.settimeout(5)
        b.settimeout(5)
        t = threading.Thread(target=lambda: self._try(auth.serve_probe, b), daemon=True)
        t.start()
        import struct
        a.sendall(struct.pack(">II", auth.PROBE_MAX + 1, 0))
        self.assertEqual(a.recv(1), b"")  # el servidor corta sin atender
        a.close()
        t.join(5)

    @staticmethod
    def _try(fn, s):
        try:
            fn(s)
        except Exception:
            pass
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()


class TestGpuSinAcelerador(unittest.TestCase):
    """nvidia-smi ve una GPU pero llama.cpp (solo CPU) no ofrece ninguna: capacidad CPU/RAM y aviso, no VRAM fantasma."""

    def test_probe_cae_a_cpu(self):
        from unittest import mock
        gpu = [{"name": "RTX 3060", "total": 12 << 30, "free": 11 << 30}]
        with mock.patch.object(hw, "nvidia_gpus", return_value=gpu):
            p = hw.Probe("auto", bw_gbps=None)
            self.assertEqual(p.backend, "gpu")
            p.set_device(None)
            self.assertEqual(p.backend, "cpu")
            self.assertIsNone(p.hardware()["gpu"])
            q = hw.Probe("gpu")  # explícito: se respeta
            q.set_device(None)
            self.assertEqual(q.backend, "gpu")

    def test_telemetria_muestra_la_gpu_sin_usar(self):
        from unittest import mock
        from dllm import telemetry
        gpu = [{"name": "RTX 3060", "vram_total": 12 << 30, "vram_used": 1 << 30, "util": 3.0}]
        with mock.patch.object(telemetry, "nvidia_smi", return_value=gpu):
            g = telemetry.Sampler(lambda: None)._gpu(None)
        self.assertTrue(g["unused"])
        self.assertEqual(g["name"], "RTX 3060")
        with mock.patch.object(telemetry, "nvidia_smi", return_value=[]):
            self.assertIsNone(telemetry.Sampler(lambda: None)._gpu(None))
