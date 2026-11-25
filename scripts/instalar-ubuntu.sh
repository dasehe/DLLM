#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# Instalación de DLLM en Ubuntu/Debian de una sola vez: dependencias, descarga, paquete y asistente.
#   Uso:  bash instalar-ubuntu.sh [opciones de install.sh, p. ej. --tui]
#   Repo público: no hace falta nada. Si vuelve a ser privado: GH_TOKEN=<token de lectura> (no se guarda en ningún sitio).
#   Variables: DLLM_SRC (carpeta de las fuentes, def. ~/dllm-src), DLLM_REPO (def. dasehe/DLLM), DLLM_REF (def. main).
set -euo pipefail

REPO="${DLLM_REPO:-dasehe/DLLM}"
REF="${DLLM_REF:-main}"
SRC="${DLLM_SRC:-$HOME/dllm-src}"

[ "$(uname -s)" = "Linux" ] || { echo "Este script es para Linux (Ubuntu/Debian)."; exit 1; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || { echo "Hace falta sudo (o ejecutar como root) para instalar git y python3."; exit 1; }
  SUDO="sudo"
fi

need=()
command -v git >/dev/null 2>&1 || need+=(git)
command -v python3 >/dev/null 2>&1 || need+=(python3)
command -v unzip >/dev/null 2>&1 || need+=(unzip)
if [ "${#need[@]}" -gt 0 ]; then
  command -v apt-get >/dev/null 2>&1 || { echo "Instala a mano: ${need[*]}"; exit 1; }
  echo "==> Instalando dependencias: ${need[*]}"
  $SUDO apt-get update -qq
  $SUDO apt-get install -y "${need[@]}"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "Se necesita Python 3.9 o superior."; exit 1; }

# El token va en una cabecera de esta orden, no en la URL: no queda en .git/config ni en el historial del shell.
git_auth() {
  if [ -n "${GH_TOKEN:-}" ]; then
    git -c "http.extraheader=Authorization: Basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 -w0)" "$@"
  else
    git "$@"
  fi
}

echo "==> Descargando $REPO ($REF) en $SRC"
if [ -d "$SRC/.git" ]; then
  git_auth -C "$SRC" fetch --quiet origin "$REF"
  git -C "$SRC" checkout --quiet FETCH_HEAD
else
  [ -e "$SRC" ] && [ -n "$(ls -A "$SRC" 2>/dev/null)" ] && { echo "$SRC existe y no es un repositorio: elige otra con DLLM_SRC."; exit 1; }
  GIT_TERMINAL_PROMPT=${GIT_TERMINAL_PROMPT:-1} git_auth clone --quiet --depth 1 --branch "$REF" "https://github.com/$REPO.git" "$SRC"
fi

echo "==> Construyendo el paquete"
cd "$SRC"
rm -rf dist
python3 scripts/build_package.py --out dist >/dev/null
PKG="$(ls -d dist/dllm-package-*/ | head -n1)"
[ -n "$PKG" ] || { echo "No se generó el paquete."; exit 1; }

# Sin escritorio (SSH): asistente en terminal salvo que el usuario indique otra cosa.
args=("$@")
if [ "${#args[@]}" -eq 0 ] && [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then args=(--tui); fi

echo "==> Lanzando el instalador ($PKG)"
cd "$PKG"
# Si se lanzó con «curl | bash», la entrada estándar es el propio script: el asistente necesita el teclado.
if [ ! -t 0 ] && { : </dev/tty; } 2>/dev/null; then exec </dev/tty; fi
exec ./install.sh "${args[@]}"
