@echo off
rem SPDX-License-Identifier: AGPL-3.0-or-later
rem Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
rem Windows: abre el panel de DLLM (lo arranca si esta parado). La consola se esconde y reaparece si cierras
rem el navegador con DLLM en marcha. Equivale al acceso directo DLLM que crea el asistente.
setlocal
cd /d "%~dp0"
title DLLM
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo No se encuentra Python: ejecuta primero Install-DLLM.bat
  pause
  exit /b 1
)
set "PYZ=%USERPROFILE%\.dllm\dllm.pyz"
if not exist "%PYZ%" set "PYZ=dllm.pyz"
%PY% "%PYZ%" open --hide-console
if errorlevel 1 pause
endlocal
