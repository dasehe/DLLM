#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# Compatibilidad: el instalador de una orden ahora es scripts/install.sh (cualquier Linux y macOS).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -n "$here" ] && [ -f "$here/install.sh" ]; then exec bash "$here/install.sh" "$@"; fi
exec bash <(curl -fsSL "https://raw.githubusercontent.com/${DLLM_REPO:-dasehe/DLLM}/${DLLM_REF:-main}/scripts/install.sh") "$@"
