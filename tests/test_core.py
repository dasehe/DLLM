# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dllm import auth, gguf, limits, scheduler  # noqa: E402
from make_gguf import write_gguf  # noqa: E402

GiB = 1 << 30


def node(i, cap_gib, bw, mbps=940, rtt=0.4, backend="cpu"):
    return {"id": i, "name": i, "backend": backend, "cap_bytes": int(cap_gib * GiB), "bw_gbps": bw,
            "mbps": mbps, "rtt_ms": rtt}


class TestGguf(unittest.TestCase):
    def test_read_model(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.gguf")
            write_gguf(p, 6 * GiB)
            m = gguf.read_model(p)
        self.assertEqual((m["arch"], m["n_layers"], m["n_embd"], m["n_head_kv"]), ("llama", 32, 4096, 8))
        self.assertEqual(m["size"], 6 * GiB)

    def test_not_gguf(self):
        with tempfile.TemporaryDirectory() as d:  # (Windows no permite reabrir un NamedTemporaryFile abierto)
            p = os.path.join(d, "x.gguf")
            with open(p, "wb") as f:
                f.write(b"nope" * 10)
            with self.assertRaises(ValueError):
                gguf.read_metadata(p)

    def test_split_size(self):
        with tempfile.TemporaryDirectory() as d:
            for i in (1, 2):
                write_gguf(os.path.join(d, f"big-0000{i}-of-00002.gguf"), GiB)
            self.assertEqual(gguf.total_size(os.path.join(d, "big-00001-of-00002.gguf")), (2 * GiB, 2))


class TestScheduler(unittest.TestCase):
    model = {"name": "m", "size": 6 * GiB, "n_layers": 32, "n_embd": 4096, "n_head": 32, "n_head_kv": 8}

    def test_fastest_first_and_minimal_nodes(self):
        p = scheduler.plan(self.model, [node("slow", 8, 20), node("fast", 8, 100), node("mid", 8, 60)])
        self.assertTrue(p["ok"])
        self.assertEqual([a["node_id"] for a in p["assignments"]], ["fast"])  # cabe en el más rápido
        self.assertTrue(any("un solo nodo" in w for w in p["warnings"]))

    def test_spreads_when_needed(self):
        p = scheduler.plan(self.model, [node("a", 4, 100), node("b", 4, 20), node("c", 3, 60)])
        self.assertTrue(p["ok"])
        self.assertEqual([a["node_id"] for a in p["assignments"]], ["a", "c", "b"])  # orden por ancho de banda
        self.assertEqual(sum(a["layers"] for a in p["assignments"]), 32)
        self.assertAlmostEqual(sum(p["tensor_split"]), 1.0, places=2)

    def test_not_enough_memory(self):
        p = scheduler.plan(self.model, [node("a", 2, 100), node("b", 2, 50)])
        self.assertFalse(p["ok"])
        self.assertIn("no cabe", p["reason"])
        self.assertIn("libre por nodo: a ", p["reason"])  # dice cuánto queda en cada nodo, no solo el total

    def test_slow_network_excluded(self):
        p = scheduler.plan(self.model, [node("lan", 8, 50), node("wifi", 8, 100, mbps=150)])
        self.assertEqual([a["node_id"] for a in p["assignments"]], ["lan"])
        self.assertTrue(any(e["node"] == "wifi" and "Mbps" in e["reason"] for e in p["excluded"]))

    def test_unmeasured_network_excluded(self):
        p = scheduler.plan(self.model, [node("a", 8, 50, mbps=None)])
        self.assertFalse(p["ok"])
        self.assertEqual(p["excluded"][0]["reason"], "red no medida")


class TestLimits(unittest.TestCase):
    hw = {"cores": 12, "ram_total": 16 * GiB, "ram_free": 12 * GiB, "gpu": None, "reserve": GiB}

    def test_share_one_third(self):
        """Ejemplo: 16 GiB y 6C/12T compartiendo 1/3 → ~5,3 GiB y 4 hilos."""
        r = limits.resolve({"share": 100 / 3}, self.hw)
        self.assertAlmostEqual(r["cap_bytes"] / GiB, 16 / 3, places=1)
        self.assertEqual(r["threads"], 4)

    def test_explicit_values_win_over_share(self):
        r = limits.resolve({"share": 50, "mem_gb": 3, "threads": 2}, self.hw)
        self.assertEqual((r["cap_bytes"], r["threads"], r["mem_source"]), (3 * GiB, 2, "fijo"))

    def test_recommended_and_ceilings(self):
        r = limits.resolve({}, self.hw)
        self.assertEqual(r["mem_source"], "recomendado")
        self.assertEqual(r["cap_bytes"], 11 * GiB)          # min(libre - reserva, 75 % del total)
        self.assertEqual(r["threads"], 6)                  # ≈ núcleos físicos
        self.assertEqual(limits.resolve({"mem_gb": 64, "threads": 99}, self.hw)["cap_bytes"], 15 * GiB)
        self.assertEqual(limits.resolve({"threads": 99}, self.hw)["threads"], 12)

    def test_gpu_pool(self):
        h = dict(self.hw, gpu={"name": "x", "total": 8 * GiB, "free": 7 * GiB})
        self.assertEqual(limits.resolve({"share": 50}, h)["cap_bytes"], 4 * GiB)  # el % se aplica a la VRAM

    def test_validate(self):
        self.assertEqual(limits.validate({"share": "25", "threads": "3"}, self.hw),
                         {"share": 25.0, "mem_gb": None, "threads": 3})
        self.assertEqual(limits.validate({"reset": True}, self.hw), {"share": None, "mem_gb": None, "threads": None})
        for bad in ({"share": 0}, {"share": 150}, {"threads": 13}, {"threads": 1.5}, {"mem_gb": 99}, {"mem_gb": "x"}):
            with self.assertRaises(ValueError):
                limits.validate(bad, self.hw)


class TestMaxContext(unittest.TestCase):
    m = {"name": "m", "size": 6 * GiB, "n_layers": 32, "n_embd": 4096, "n_head": 32, "n_head_kv": 8,
         "ctx_train": 32768}

    def test_more_memory_more_context(self):
        small, big = scheduler.max_context(self.m, 8 * GiB), scheduler.max_context(self.m, 9 * GiB)
        self.assertGreater(big, small)
        self.assertEqual(small % 256, 0)
        self.assertEqual(scheduler.max_context(self.m, 100 * GiB), 32768)  # tope: contexto de entrenamiento
        self.assertIsNone(scheduler.max_context(self.m, 6 * GiB))

    def test_plan_reports_max_ctx_and_warns_over_training(self):
        nodes = [node("a", 16, 50)]
        p = scheduler.plan(self.m, nodes, ctx=4096)
        self.assertTrue(p["ok"])
        self.assertGreater(p["max_ctx"], 4096)
        p = scheduler.plan(self.m, nodes, ctx=65536)
        self.assertTrue(any("entrenamiento" in w for w in p["warnings"]))
        p = scheduler.plan(self.m, [node("a", 8, 50)], ctx=32768)
        self.assertFalse(p["ok"])
        self.assertIn("contexto de hasta", p["reason"])


class TestSlidingWindowKV(unittest.TestCase):
    """Gemma 4 31B (config de Google): 60 capas, 50 SWA (ventana 1024, 16 cabezas KV de 256) y 10 globales
    (4 cabezas KV de 512). Antes se estimaba como si todas guardaran todo el contexto."""

    def gemma4(self):
        meta = {"gemma4.attention.head_count_kv": [16] * 5 * 10, "gemma4.attention.key_length": 512,
                "gemma4.attention.value_length": 512, "gemma4.attention.key_length_swa": 256,
                "gemma4.attention.value_length_swa": 256, "gemma4.attention.sliding_window": 1024,
                "gemma4.attention.sliding_window_pattern": ([True] * 5 + [False]) * 10}
        meta["gemma4.attention.head_count_kv"] = ([16] * 5 + [4]) * 10
        layers = gguf.kv_layout(meta, "gemma4", 60, 5376, 32)
        return {"size": int(17.4 * GiB), "n_layers": 60, "n_embd": 5376, "n_head": 32, "n_head_kv": 16,
                "ctx_train": 262144, "kv_layers": layers, "n_swa": 1024}

    def test_gemma4_kv(self):
        m = self.gemma4()
        self.assertEqual(sum(1 for x in m["kv_layers"] if x[3]), 50)
        kv = scheduler.kv_bytes(m, 65536)
        glob = 10 * 4 * 512 * 2 * 2 * 65536          # capas globales: todo el contexto
        swa = 50 * 16 * 256 * 2 * 2 * 1536            # capas SWA: ventana + lote
        self.assertEqual(kv, glob + swa)
        self.assertLess(kv, 7 * GiB)                  # antes: ~78.8 GiB
        # con 22.1 GiB de los dos nodos del usuario cabe mucho más que los 2816 tokens de antes
        self.assertGreater(scheduler.max_context(m, int(22.1 * GiB)), 16384)

    def test_swa_cells_and_shared(self):
        self.assertEqual(scheduler.swa_cells(512, 1024), 512)        # contexto menor que la ventana
        self.assertEqual(scheduler.swa_cells(65536, 1024), 1536)
        meta = {"x.attention.head_count_kv": 8, "x.attention.shared_kv_layers": 2}
        self.assertEqual(len(gguf.kv_layout(meta, "x", 10, 4096, 32)), 8)
        # gemma3 sin patrón en el GGUF: 5 SWA y 1 global
        meta = {"gemma3.attention.head_count_kv": 8, "gemma3.attention.sliding_window": 1024}
        lay = gguf.kv_layout(meta, "gemma3", 12, 4096, 32)
        self.assertEqual([x[3] for x in lay], ([True] * 5 + [False]) * 2)
        # modelo normal: igual que la fórmula clásica
        m = {"size": GiB, "n_layers": 32, "n_embd": 4096, "n_head": 32, "n_head_kv": 8,
             "kv_layers": gguf.kv_layout({"llama.attention.head_count_kv": 8}, "llama", 32, 4096, 32)}
        self.assertEqual(scheduler.kv_bytes(m, 4096), 32 * 4096 * 8 * 128 * 4)


class TestAuth(unittest.TestCase):
    def _pair(self, s_secret, c_secret):
        a, b = socket.socketpair()
        a.settimeout(3)
        b.settimeout(3)
        res = {}

        def srv():
            try:
                auth.server_handshake(a, s_secret)
                res["srv"] = "ok"
            except Exception as e:
                res["srv"] = type(e).__name__

        t = threading.Thread(target=srv)
        t.start()
        try:
            auth.client_handshake(b, c_secret)
            res["cli"] = "ok"
        except Exception as e:
            res["cli"] = type(e).__name__
        t.join()
        return res

    def test_handshake_ok(self):
        self.assertEqual(self._pair("s3cret", "s3cret"), {"srv": "ok", "cli": "ok"})

    def test_handshake_wrong_secret(self):
        r = self._pair("s3cret", "otro")
        self.assertNotEqual(r["srv"], "ok")
        self.assertNotEqual(r["cli"], "ok")

    def test_signed_request(self):
        bh = auth.body_hash(b"x")
        ts, sig = auth.sign_request("k", "POST", "/p", bh)
        self.assertTrue(auth.verify_request("k", "POST", "/p", bh, ts, sig))
        self.assertFalse(auth.verify_request("k", "POST", "/otra", bh, ts, sig))
        self.assertFalse(auth.verify_request("k2", "POST", "/p", bh, ts, sig))
        old_ts, old_sig = auth.sign_request("k", "POST", "/p", bh, ts=int(ts) - 3600)
        self.assertFalse(auth.verify_request("k", "POST", "/p", bh, old_ts, old_sig))

    def test_beacon(self):
        msg = auth.sign_beacon("k", {"name": "n", "http_port": 1, "ts": __import__("time").time()})
        self.assertEqual(auth.verify_beacon("k", msg)["name"], "n")
        self.assertIsNone(auth.verify_beacon("otro", msg))


class TestHTTPServer(unittest.TestCase):
    def test_no_reverse_dns_on_bind(self):
        """HTTPServer llama a socket.getfqdn al arrancar; en macOS puede tardar 30 s. FastHTTPServer no debe."""
        import http.server
        from unittest import mock
        from dllm.util import FastHTTPServer
        with mock.patch("socket.getfqdn", side_effect=AssertionError("getfqdn no debe llamarse")):
            srv = FastHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        self.assertEqual(srv.server_name, "127.0.0.1")
        srv.server_close()


if __name__ == "__main__":
    unittest.main()
