# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Genera un GGUF 'falso' (solo cabecera válida + relleno disperso) para probar sin pesos reales."""
import struct


def _s(s):
    b = s.encode()
    return struct.pack("<Q", len(b)) + b


def _kv_str(k, v):
    return _s(k) + struct.pack("<I", 8) + _s(v)


def _kv_u32(k, v):
    return _s(k) + struct.pack("<I", 4) + struct.pack("<I", v)


def write_gguf(path, size_bytes, n_layers=32, n_embd=4096, n_head=32, n_head_kv=8, ctx=8192, arch="llama"):
    kvs = [_kv_str("general.architecture", arch), _kv_str("general.name", "mock-model"),
           _kv_u32(f"{arch}.block_count", n_layers), _kv_u32(f"{arch}.embedding_length", n_embd),
           _kv_u32(f"{arch}.attention.head_count", n_head), _kv_u32(f"{arch}.attention.head_count_kv", n_head_kv),
           _kv_u32(f"{arch}.context_length", ctx),
           # array de strings (como el vocabulario): el lector debe saltárselo
           _s("tokenizer.ggml.tokens") + struct.pack("<IIQ", 9, 8, 3) + _s("a") + _s("bb") + _s("ccc")]
    with open(path, "wb") as f:
        f.write(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)))
        for kv in kvs:
            f.write(kv)
        f.truncate(size_bytes)  # fichero disperso: ocupa ~0 en disco pero mide size_bytes
