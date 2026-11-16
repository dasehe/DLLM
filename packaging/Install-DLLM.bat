@echo off
rem SPDX-License-Identifier: AGPL-3.0-or-later
rem Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
rem Windows: doble clic para abrir el asistente de instalacion de DLLM.
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Instalador de DLLM

set "PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=python"
  )
)

if not defined PY (
  echo DLLM necesita Python 3.9 o superior y no se ha encontrado.
  where winget >nul 2>nul
  if errorlevel 1 (
    echo Instalalo desde https://www.python.org/downloads/ y vuelve a ejecutar este archivo.
    pause
    exit /b 1
  )
  set "ANS="
  set /p ANS="Instalar Python ahora con winget? [S/n] "
  if /i "!ANS!"=="n" exit /b 1
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python instalado. Cierra esta ventana y vuelve a ejecutar Install-DLLM.bat
  pause
  exit /b 0
)

%PY% dllm.pyz setup --hide-console %*
if errorlevel 1 pause
endlocal
