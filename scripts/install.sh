#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# Instalación de DLLM en una sola orden para Linux (Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE, Alpine) y macOS:
# instala Python si falta, descarga el código (tarball de GitHub, sin git), construye el paquete y abre el asistente.
#   bash <(curl -fsSL https://raw.githubusercontent.com/dasehe/DLLM/main/scripts/install.sh)
#   Opciones: se pasan a install.sh (p. ej. --tui). Variables: DLLM_SRC (def. ~/dllm-src), DLLM_REPO (def. dasehe/DLLM),
#   DLLM_REF (rama o etiqueta, def. main), DLLM_LANG (en|es; por defecto según el idioma del sistema),
#   GH_TOKEN (solo si el repositorio fuese privado; no se guarda en ningún sitio).
set -euo pipefail

REPO="${DLLM_REPO:-dasehe/DLLM}"
REF="${DLLM_REF:-main}"
SRC="${DLLM_SRC:-$HOME/dllm-src}"
LANG_UI="${DLLM_LANG:-$(case "${LC_ALL:-${LANG:-}}" in es*) echo es ;; *) echo en ;; esac)}"
export DLLM_LANG="$LANG_UI"

t() { if [ "$LANG_UI" = "es" ]; then printf '%s\n' "$2"; else printf '%s\n' "$1"; fi; }
die() { t "$1" "$2" >&2; exit 1; }

OS="$(uname -s)"
case "$OS" in Linux|Darwin) ;; *) die "Unsupported system: $OS (use install.ps1 on Windows)." \
                                      "Sistema no compatible: $OS (en Windows usa install.ps1)." ;; esac

SUDO=""
if [ "$(id -u)" -ne 0 ] && [ "$OS" = "Linux" ]; then
  command -v sudo >/dev/null 2>&1 && SUDO="sudo"
fi

pkg_install() {  # instala paquetes con el gestor disponible
  if   command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update -qq && $SUDO apt-get install -y "$@"
  elif command -v dnf     >/dev/null 2>&1; then $SUDO dnf install -y "$@"
  elif command -v yum     >/dev/null 2>&1; then $SUDO yum install -y "$@"
  elif command -v pacman  >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm "$@"
  elif command -v zypper  >/dev/null 2>&1; then $SUDO zypper --non-interactive install "$@"
  elif command -v apk     >/dev/null 2>&1; then $SUDO apk add "$@"
  elif command -v brew    >/dev/null 2>&1; then brew install "$@"
  else return 1; fi
}

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  t "==> Installing Python 3" "==> Instalando Python 3"
  if command -v pacman >/dev/null 2>&1; then pkg_install python; elif command -v brew >/dev/null 2>&1; then pkg_install python
  else pkg_install python3; fi || die "Install Python 3.9+ from https://www.python.org/downloads/ and run this again." \
                                      "Instala Python 3.9+ desde https://www.python.org/downloads/ y vuelve a ejecutarlo."
  PY=python3
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 or newer is required." "Se necesita Python 3.9 o superior."

DL=""
if command -v curl >/dev/null 2>&1; then DL="curl"; elif command -v wget >/dev/null 2>&1; then DL="wget"
else pkg_install curl && DL="curl" || die "curl or wget is required." "Hace falta curl o wget."; fi
command -v tar >/dev/null 2>&1 || pkg_install tar || die "tar is required." "Hace falta tar."

fetch() {  # fetch URL DESTINO — el token, si lo hay, va en una cabecera (no en la URL ni en el historial)
  local auth=()
  [ -n "${GH_TOKEN:-}" ] && auth=(-H "Authorization: token $GH_TOKEN")
  if [ "$DL" = "curl" ]; then curl -fsSL "${auth[@]}" -o "$2" "$1"
  else wget -q ${GH_TOKEN:+--header="Authorization: token $GH_TOKEN"} -O "$2" "$1"; fi
}

t "==> Downloading $REPO ($REF) into $SRC" "==> Descargando $REPO ($REF) en $SRC"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fetch "https://codeload.github.com/$REPO/tar.gz/$REF" "$TMP/src.tgz" \
  || die "Download failed (check the network, DLLM_REPO and DLLM_REF)." \
         "Falló la descarga (revisa la red, DLLM_REPO y DLLM_REF)."
mkdir -p "$TMP/x"
tar -xzf "$TMP/src.tgz" -C "$TMP/x"
TOP="$(find "$TMP/x" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ -f "$TOP/scripts/build_package.py" ] || die "The download does not look like DLLM." "La descarga no parece DLLM."
if [ -e "$SRC" ] && [ ! -f "$SRC/scripts/build_package.py" ] && [ -n "$(ls -A "$SRC" 2>/dev/null)" ]; then
  die "$SRC exists and is not a DLLM folder: choose another one with DLLM_SRC." \
      "$SRC existe y no es una carpeta de DLLM: elige otra con DLLM_SRC."
fi
rm -rf "$SRC"
mv "$TOP" "$SRC"

t "==> Building the package" "==> Construyendo el paquete"
cd "$SRC"
"$PY" scripts/build_package.py --out dist >/dev/null
PKG="$(ls -d dist/dllm-package-*/ 2>/dev/null | head -n1)"
[ -n "$PKG" ] || die "The package was not built." "No se generó el paquete."

# Sin escritorio (SSH): asistente en terminal salvo que se indique otra cosa.
args=("$@")
if [ "${#args[@]}" -eq 0 ] && [ "$OS" = "Linux" ] && [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then args=(--tui); fi

t "==> Starting the installer ($PKG)" "==> Lanzando el instalador ($PKG)"
cd "$PKG"
# Con «curl | bash» la entrada estándar es el propio script: el asistente necesita el teclado.
if [ ! -t 0 ] && { : </dev/tty; } 2>/dev/null; then exec </dev/tty; fi
exec ./install.sh "${args[@]}"
