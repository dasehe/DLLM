#!/usr/bin/env sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# Desinstala DLLM de este equipo (Linux y macOS). Ejecuta: ./uninstall.sh
set -e
cd "$(dirname "$0")"
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then exec "$c" dllm.pyz uninstall --gui "$@"; fi
done
echo "No se encuentra Python: DLLM no puede estar en marcha en este equipo."
exit 1
