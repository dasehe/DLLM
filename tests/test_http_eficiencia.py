# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Eficiencia del panel: gzip y ETag para la página, telemetría incremental (?since). Orquestador real con binarios
simulados."""
import gzip
import http.client
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_nodes_admin  # noqa: E402


class Fixture(test_nodes_admin.NodesAdminE2E):
    """Reutiliza el arranque del clúster simulado sin repetir sus tests."""
    pass


for _n in [n for n in dir(Fixture) if n.startswith("test")]:
    setattr(Fixture, _n, None)


class HttpEficiencia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Fixture.setUpClass()
        cls.op = Fixture.op

    @classmethod
    def tearDownClass(cls):
        Fixture.tearDownClass()

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.op, timeout=30)
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def test_pagina_gzip_y_etag(self):
        st, h, plain = self.get("/")
        self.assertEqual(st, 200)
        self.assertNotIn("Content-Encoding", h)
        st, hz, body = self.get("/", {"Accept-Encoding": "gzip, br"})
        self.assertEqual(hz.get("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(body), plain)
        self.assertLess(len(body), len(plain) // 3)
        st, h304, b = self.get("/", {"If-None-Match": hz["ETag"]})
        self.assertEqual((st, b), (304, b""))

    def test_json_comprimido_y_v1_no(self):
        st, h, body = self.get("/api/state", {"Accept-Encoding": "gzip"})
        data = json.loads(gzip.decompress(body) if h.get("Content-Encoding") == "gzip" else body)
        self.assertIn("state", data)
        st, h, body = self.get("/v1/models", {"Accept-Encoding": "gzip"})
        self.assertNotIn("Content-Encoding", h)

    def test_telemetria_incremental(self):
        deadline = time.time() + 30
        while True:
            full = json.loads(self.get("/api/telemetry")[2])
            if any(n["hist"] for n in full["nodes"]) or time.time() > deadline:
                break
            time.sleep(1)
        self.assertFalse(full["delta"])
        self.assertTrue(any(n["hist"] for n in full["nodes"]))
        d = json.loads(self.get(f"/api/telemetry?since={full['t']}")[2])
        self.assertTrue(d["delta"])
        for n in d["nodes"]:
            self.assertTrue(all(x[0] > round(full["t"], 1) - 0.1 for x in n["hist"]))
        self.assertLessEqual(sum(len(n["hist"]) for n in d["nodes"]), sum(len(n["hist"]) for n in full["nodes"]))
        self.assertEqual(self.get("/api/telemetry?since=abc")[0], 400)


if __name__ == "__main__":
    unittest.main()
