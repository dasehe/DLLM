@echo off
rem SPDX-License-Identifier: AGPL-3.0-or-later
rem Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
rem Windows: doble clic para desinstalar DLLM de este equipo (agente, orquestador o ambos).
setlocal
cd /d "%~dp0"
title Desinstalar DLLM
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo No se encuentra Python: DLLM no puede estar en marcha en este equipo.
  pause
  exit /b 1
)
%PY% dllm.pyz uninstall --interactive
pause
endlocal
