#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# macOS: doble clic para desinstalar DLLM de este equipo.
cd "$(dirname "$0")"
xattr -dr com.apple.quarantine . 2>/dev/null || true
chmod +x ./uninstall.sh
./uninstall.sh
printf "Pulsa Intro para cerrar… "; read -r _
