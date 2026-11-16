# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Lector mínimo de cabecera GGUF: solo metadatos escalares necesarios para planificar."""
import os
import re
import struct

_FIXED = {0: ("B", 1), 1: ("b", 1), 2: ("H", 2), 3: ("h", 2), 4: ("I", 4), 5: ("i", 4),
          6: ("f", 4), 7: ("?", 1), 10: ("Q", 8), 11: ("q", 8), 12: ("d", 8)}


def _u32(f):
    return struct.unpack("<I", f.read(4))[0]


def _u64(f):
    return struct.unpack("<Q", f.read(8))[0]


def _str(f):
    return f.read(_u64(f)).decode("utf-8", "replace")


def _value(f, t):
    if t in _FIXED:
        fmt, sz = _FIXED[t]
        return struct.unpack("<" + fmt, f.read(sz))[0]
    if t == 8:
        return _str(f)
    if t == 9:  # array: los pequeños de números se leen (valores por capa); el resto se salta (vocabularios)
        et, n = _u32(f), _u64(f)
        if et in _FIXED and n <= 4096:
            fmt, sz = _FIXED[et]
            return list(struct.unpack("<" + fmt * n, f.read(sz * n)))
        if et in _FIXED:
            f.seek(n * _FIXED[et][1], 1)
        elif et == 8:
            for _ in range(n):
                f.seek(_u64(f), 1)
        else:
            for _ in range(n):
                _value(f, et)
        return None
    raise ValueError(f"tipo GGUF desconocido: {t}")


def read_metadata(path):
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError(f"{os.path.basename(path)} no es un fichero GGUF")
        version = _u32(f)
        if version < 2:
            raise ValueError(f"versión GGUF {version} no soportada")
        _u64(f)  # tensor_count
        meta = {}
        for _ in range(_u64(f)):
            key = _str(f)
            val = _value(f, _u32(f))
            if val is not None:
                meta[key] = val
        return meta


_SPLIT = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$")


def total_size(path):
    """Tamaño total, sumando partes si el modelo está troceado (-00001-of-0000N.gguf)."""
    m = _SPLIT.match(os.path.basename(path))
    if not m:
        return os.path.getsize(path), 1
    base, _, n = m.group(1), m.group(2), int(m.group(3))
    d = os.path.dirname(path)
    total = 0
    for i in range(1, n + 1):
        total += os.path.getsize(os.path.join(d, f"{base}-{i:05d}-of-{n:05d}.gguf"))
    return total, n


# Arquitecturas con atención de ventana deslizante que no guardan el patrón en el GGUF: cada cuántas capas hay una
# global (el resto son SWA). Las que sí lo guardan usan {arch}.attention.sliding_window_pattern (por capa).
SWA_EVERY = {"gemma2": 2, "gemma3": 6, "gemma3n": 5, "cohere2": 4}


def kv_layout(meta, arch, n_layers, n_embd, n_head):
    """Por cada capa con caché propia: (n_head_kv, dim_k, dim_v, es_swa). Lo que necesita la caché KV de verdad:
    - n_head_kv y dimensiones de cabeza pueden ser por capa o distintas en capas SWA (Gemma 4);
    - las capas SWA solo guardan la ventana, no todo el contexto;
    - las últimas capas pueden reutilizar la caché de otra (shared_kv_layers) y no ocupan nada."""
    g = lambda k, d=None: meta.get(f"{arch}.{k}", d)
    if not (n_layers and n_head):
        return None
    per = lambda v, i, d: (v[i] if isinstance(v, list) and i < len(v) else (v if isinstance(v, (int, float)) else d))
    hkv = g("attention.head_count_kv", n_head)
    hd = (n_embd // n_head) if n_embd else None
    dk, dv = g("attention.key_length", hd), g("attention.value_length", hd)
    dk_swa, dv_swa = g("attention.key_length_swa", dk), g("attention.value_length_swa", dv)
    pattern = g("attention.sliding_window_pattern")
    every = SWA_EVERY.get(arch)
    n_own = n_layers - int(g("attention.shared_kv_layers", 0) or 0)
    out = []
    for i in range(max(0, n_own)):
        if isinstance(pattern, list) and i < len(pattern):
            swa = bool(pattern[i])
        else:
            swa = bool(g("attention.sliding_window")) and bool(every) and (i + 1) % every != 0
        h = per(hkv, i, n_head)
        if not h:  # 0 cabezas KV en esa capa (capas sin atención en algunos modelos híbridos)
            continue
        out.append((int(h), int(dk_swa if swa else dk or 0), int(dv_swa if swa else dv or 0), swa))
    return out if out and all(x[1] and x[2] for x in out) else None


def read_model(path):
    meta = read_metadata(path)
    arch = meta.get("general.architecture", "llama")
    g = lambda k, d=None: meta.get(f"{arch}.{k}", d)
    size, parts = total_size(path)
    n_head = g("attention.head_count")
    if isinstance(n_head, list):  # por capa: se usa la mayor
        n_head = max(n_head) or None
    return {
        "path": path,
        "name": meta.get("general.name") or os.path.basename(path),
        "size": size,
        "parts": parts,
        "arch": arch,
        "n_layers": g("block_count"),
        "n_embd": g("embedding_length"),
        "n_head": n_head,
        "n_head_kv": g("attention.head_count_kv", n_head),
        "ctx_train": g("context_length"),
        "kv_layers": kv_layout(meta, arch, g("block_count"), g("embedding_length"), n_head),
        "n_swa": g("attention.sliding_window"),
    }
