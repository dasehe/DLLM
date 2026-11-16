#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# macOS: doble clic para abrir el asistente de instalación de DLLM.
cd "$(dirname "$0")"
xattr -dr com.apple.quarantine . 2>/dev/null || true   # el zip descargado viene marcado por Gatekeeper
chmod +x ./install.sh
./install.sh "$@"
echo
read -r -p "Pulsa Enter para cerrar esta ventana…" _
