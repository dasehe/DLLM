# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Reparto con GPU + RAM del mismo nodo y prioridad por nodo (planificador)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dllm import scheduler  # noqa: E402

GiB = 1 << 30
# como el caso real: 31B a 17.4 GiB, ctx 8128 → ~20 GiB
MODEL = {"name": "gemma", "size": int(17.4 * GiB), "n_layers": 60, "n_embd": 5376, "n_head": 32, "n_head_kv": 6}


def gpu(i, vram, ram, local=False, rtt=0.4, prio=0):
    net = {"mbps": 10000, "rtt_ms": rtt, "local": local, "priority": prio}
    return [dict(id=i, host=i, name=i, backend="cuda", device="gpu", gpu=True, cap_bytes=int(vram * GiB),
                 bw_gbps=360, **net),
            dict(id=i + "#ram", host=i, name=i, aux=True, backend="cpu", device="cpu", cap_bytes=int(ram * GiB),
                 overhead=256 << 20, bw_gbps=20, **net)]


def cpu(i, ram, local=False, rtt=0.4, prio=0):
    return [dict(id=i, host=i, name=i, backend="cpu", device="cpu", cap_bytes=int(ram * GiB), bw_gbps=20,
                 mbps=10000, rtt_ms=rtt, local=local, priority=prio)]


class GpuRam(unittest.TestCase):
    def test_caso_real_gpu_y_ram_del_mismo_nodo_antes_que_otro_equipo(self):
        nodes = gpu("vAI1", 9.6, 14.5, local=True, rtt=0.05) + cpu("vAI2", 8.3, rtt=0.08)
        p = scheduler.plan(MODEL, nodes, ctx=8128)
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual([(a["host_id"], a["device"]) for a in p["assignments"]], [("vAI1", "gpu"), ("vAI1", "cpu")])
        self.assertEqual(sum(a["layers"] for a in p["assignments"]), 60)
        self.assertNotIn("el modelo cabe en un solo nodo", " ".join(p["warnings"]))

    def test_sin_ram_suficiente_pasa_al_siguiente_nodo_al_final(self):
        nodes = cpu("vAI2", 8.3, rtt=0.08) + gpu("vAI1", 9.6, 5, rtt=0.05)
        p = scheduler.plan(MODEL, nodes, ctx=8128)
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual([(a["host_id"], a["device"]) for a in p["assignments"]],
                         [("vAI1", "gpu"), ("vAI1", "cpu"), ("vAI2", "cpu")])  # mismo equipo junto, GPU primero

    def test_cabe_en_vram_no_usa_ram(self):
        small = dict(MODEL, size=6 * GiB)
        p = scheduler.plan(small, gpu("g", 12, 14, local=True) + cpu("c", 8), ctx=2048)
        self.assertEqual([(a["host_id"], a["device"]) for a in p["assignments"]], [("g", "gpu")])

    def test_prioridad_manda(self):
        # dos nodos remotos iguales: sin prioridad elige el más rápido; con prioridad, el elegido primero
        nodes = gpu("a", 9.6, 14.5) + cpu("b", 30)
        p = scheduler.plan(MODEL, nodes, ctx=8128)
        self.assertEqual(p["assignments"][0]["host_id"], "a")
        nodes = gpu("a", 9.6, 14.5) + cpu("b", 30, prio=5)
        p = scheduler.plan(MODEL, nodes, ctx=8128)
        self.assertEqual([a["host_id"] for a in p["assignments"]], ["b"])  # cabe entero en b, que tiene prioridad
        self.assertEqual(p["assignments"][0]["priority"], 5)

    def test_un_salto_por_equipo(self):
        p = scheduler.plan(MODEL, gpu("r", 9.6, 14.5, rtt=2.0), ctx=8128)
        self.assertTrue(p["ok"], p["reason"])
        # GPU y RAM del mismo equipo: un salto de red, no dos
        t = scheduler.HOST_MS / 1000 + 2.0 / 1000 + sum(
            a["share"] * MODEL["size"] / (a["bw_gbps"] * 1e9 * scheduler.EFFICIENCY) for a in p["assignments"])
        self.assertAlmostEqual(p["est_tok_s"], round(1 / t, 2), places=2)

    def test_mensaje_no_cabe_distingue_ram(self):
        p = scheduler.plan(MODEL, gpu("vAI1", 4, 3) + cpu("vAI2", 2), ctx=8128)
        self.assertFalse(p["ok"])
        self.assertIn("vAI1 (RAM)", p["reason"])


class GpuAntesQueRam(unittest.TestCase):
    def test_todas_las_gpu_antes_que_cualquier_ram(self):
        # 2 nodos con GPU y RAM: el modelo no cabe en una GPU → GPU1 + GPU2, sin tocar la RAM
        nodes = gpu("a", 12, 14, local=True, rtt=0.05) + gpu("b", 12, 14, rtt=0.3)
        p = scheduler.plan(MODEL, nodes, ctx=8128)
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual([(a["host_id"], a["device"]) for a in p["assignments"]], [("a", "gpu"), ("b", "gpu")])

    def test_ram_solo_cuando_no_caben_todas_las_gpu(self):
        nodes = gpu("a", 6, 14, local=True, rtt=0.05) + gpu("b", 6, 14, rtt=0.3) + gpu("c", 5, 14, rtt=0.3)
        p = scheduler.plan(MODEL, nodes, ctx=8128)
        self.assertTrue(p["ok"], p["reason"])
        gpus = [a for a in p["assignments"] if a["device"] == "gpu"]
        self.assertEqual(len(gpus), 3)                       # las tres GPU llenas primero
        ram = sum(a["bytes"] for a in p["assignments"] if a["device"] == "cpu")
        self.assertLess(ram, 6 * GiB)                        # y solo lo que falta en RAM (~5.5 GiB)
        hosts = [a["host_id"] for a in p["assignments"]]
        self.assertEqual(hosts, sorted(hosts, key=hosts.index))  # cada equipo junto (un rpc-server por equipo)

    def test_granja_de_gpus_voraz(self):
        nodes = []
        for i in range(10):  # 20 entradas: reparto voraz, también GPU antes que RAM
            nodes += gpu(f"g{i}", 3, 14, rtt=0.3)
        p = scheduler.plan(MODEL, nodes, ctx=8128)
        self.assertTrue(p["ok"], p["reason"])
        self.assertTrue(all(a["device"] == "gpu" for a in p["assignments"]))


class RpcMultiDevice(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "binarios simulados POSIX")
    def test_detecta_rpc_server_con_varios_dispositivos(self):
        from dllm import devices
        rpc = os.path.join(os.path.dirname(__file__), "mockbin", "rpc-server")
        self.assertTrue(devices.rpc_multi_device(rpc))
        os.environ["MOCK_RPC_SINGLE"] = "1"
        try:
            self.assertFalse(devices.rpc_multi_device(rpc))  # rpc-server antiguo: el nodo presta solo su GPU
        finally:
            del os.environ["MOCK_RPC_SINGLE"]


if __name__ == "__main__":
    unittest.main()
