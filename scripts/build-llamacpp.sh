#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# Compila llama.cpp con soporte RPC. Usar la MISMA build en el orquestador y en todos los nodos.
# Uso: ./build-llamacpp.sh [cuda|vulkan|cpu]   (por defecto cpu)  ·  salida en ./llama.cpp/build/bin
set -euo pipefail
BACKEND="${1:-cpu}"
sudo apt-get install -y git cmake build-essential libcurl4-openssl-dev >/dev/null 2>&1 || true
[ -d llama.cpp ] || git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
[ -n "${LLAMA_REF:-}" ] && git checkout "$LLAMA_REF"   # fija una versión para que todos los nodos coincidan
FLAGS="-DGGML_RPC=ON -DCMAKE_BUILD_TYPE=Release"
case "$BACKEND" in
  cuda)   FLAGS="$FLAGS -DGGML_CUDA=ON" ;;
  vulkan) FLAGS="$FLAGS -DGGML_VULKAN=ON" ;;
esac
cmake -B build $FLAGS
# el servidor RPC se llama ggml-rpc-server en llama.cpp reciente y rpc-server en versiones antiguas
cmake --build build --config Release -j"$(nproc)" --target llama-server ggml-rpc-server \
  || cmake --build build --config Release -j"$(nproc)" --target llama-server rpc-server
echo "Listo: $(pwd)/build/bin  ->  dllm agent --bin-dir $(pwd)/build/bin ..."
git rev-parse --short HEAD
