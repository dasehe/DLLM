# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Los tests comprueban los mensajes en su idioma fuente (español): se fija DLLM_LANG=es para toda la ejecución
(este módulo se importa el primero). Las salidas en inglés, idioma por defecto, se prueban en test_i18n.py."""
import os
import unittest

os.environ["DLLM_LANG"] = "es"


class Idioma(unittest.TestCase):
    def test_idioma_de_los_tests(self):
        self.assertEqual(os.environ["DLLM_LANG"], "es")
