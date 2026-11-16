#!/usr/bin/env sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# Instalador de DLLM para Linux y macOS. Ejecuta: ./install.sh   (añade --tui para el asistente en terminal)
set -e
cd "$(dirname "$0")"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done

if [ -z "$PY" ]; then
  echo "DLLM necesita Python 3.9 o superior y no se ha encontrado."
  CMD=""
  if   command -v apt-get >/dev/null 2>&1; then CMD="sudo apt-get install -y python3"
  elif command -v dnf     >/dev/null 2>&1; then CMD="sudo dnf install -y python3"
  elif command -v pacman  >/dev/null 2>&1; then CMD="sudo pacman -S --noconfirm python"
  elif command -v zypper  >/dev/null 2>&1; then CMD="sudo zypper install -y python3"
  elif command -v brew    >/dev/null 2>&1; then CMD="brew install python"
  fi
  if [ -z "$CMD" ]; then
    echo "Instálalo desde https://www.python.org/downloads/ y vuelve a ejecutar este script."; exit 1
  fi
  printf "¿Instalar Python con '%s'? [S/n] " "$CMD"
  read -r ans
  case "$ans" in n|N) exit 1 ;; esac
  sh -c "$CMD"
  PY=python3
fi

exec "$PY" dllm.pyz setup "$@"
