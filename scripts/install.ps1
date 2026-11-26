# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
# Instalación de DLLM en Windows con una sola orden de PowerShell (5.1 o 7):
#   irm https://raw.githubusercontent.com/dasehe/DLLM/main/scripts/install.ps1 | iex
# Instala Python con winget si falta, descarga el código (zip de GitHub, sin git), construye el paquete y abre el
# asistente. Variables: DLLM_SRC (def. %USERPROFILE%\dllm-src), DLLM_REPO (def. dasehe/DLLM), DLLM_REF (def. main),
# DLLM_LANG (en|es; por defecto según el idioma de Windows), GH_TOKEN (solo si el repositorio fuese privado).
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = if ($env:DLLM_REPO) { $env:DLLM_REPO } else { 'dasehe/DLLM' }
$Ref  = if ($env:DLLM_REF)  { $env:DLLM_REF }  else { 'main' }
$Src  = if ($env:DLLM_SRC)  { $env:DLLM_SRC }  else { Join-Path $env:USERPROFILE 'dllm-src' }
$Lang = if ($env:DLLM_LANG) { $env:DLLM_LANG } elseif ((Get-UICulture).Name -like 'es*') { 'es' } else { 'en' }
$env:DLLM_LANG = $Lang

function T([string]$en, [string]$es) { if ($Lang -eq 'es') { $es } else { $en } }
function Say([string]$en, [string]$es) { Write-Host (T $en $es) -ForegroundColor Cyan }
function Fail([string]$en, [string]$es) { Write-Host (T $en $es) -ForegroundColor Red; throw 'DLLM' }

function Find-Python {
  # py -3 primero; "python" puede ser el alias de la Microsoft Store (no es Python real): se comprueba la versión
  foreach ($c in @(@('py', '-3'), @('python'))) {
    $exe = Get-Command $c[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $args_ = @($c | Select-Object -Skip 1) + @('-c', 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)')
    try { & $c[0] @args_ 2>$null; if ($LASTEXITCODE -eq 0) { return , $c } } catch { }
  }
  return $null
}

try {
  $py = Find-Python
  if (-not $py) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
      Fail 'Python 3.9+ is required: install it from https://www.python.org/downloads/ (tick "Add to PATH") and run this again.' `
           'Hace falta Python 3.9+: instálalo desde https://www.python.org/downloads/ (marca "Add to PATH") y vuelve a ejecutarlo.'
    }
    Say '==> Installing Python 3.12 with winget' '==> Instalando Python 3.12 con winget'
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements | Out-Host
    # PATH nuevo sin cerrar la ventana
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $py = Find-Python
    if (-not $py) { Fail 'Python was installed but is not visible yet: open a new PowerShell window and run the command again.' `
                         'Python se instaló pero aún no es visible: abre otra ventana de PowerShell y repite la orden.' }
  }

  Say "==> Downloading $Repo ($Ref) into $Src" "==> Descargando $Repo ($Ref) en $Src"
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ('dllm-' + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  $zip = Join-Path $tmp 'src.zip'
  $headers = @{}
  if ($env:GH_TOKEN) { $headers['Authorization'] = "token $($env:GH_TOKEN)" }
  $ProgressPreference = 'SilentlyContinue'  # la barra de progreso de PowerShell 5 hace la descarga muy lenta
  Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri "https://codeload.github.com/$Repo/zip/$Ref" -OutFile $zip
  Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp 'x') -Force
  $top = Get-ChildItem -Directory (Join-Path $tmp 'x') | Select-Object -First 1
  if (-not $top -or -not (Test-Path (Join-Path $top.FullName 'scripts\build_package.py'))) {
    Fail 'The download does not look like DLLM.' 'La descarga no parece DLLM.'
  }
  if ((Test-Path $Src) -and -not (Test-Path (Join-Path $Src 'scripts\build_package.py')) -and (Get-ChildItem $Src -Force | Select-Object -First 1)) {
    Fail "$Src exists and is not a DLLM folder: choose another one with DLLM_SRC." "$Src existe y no es una carpeta de DLLM: elige otra con DLLM_SRC."
  }
  if (Test-Path $Src) { Remove-Item -Recurse -Force $Src }
  Move-Item $top.FullName $Src
  Remove-Item -Recurse -Force $tmp

  Say '==> Building the package' '==> Construyendo el paquete'
  Push-Location $Src
  $pyArgs = @($py | Select-Object -Skip 1)
  & $py[0] @pyArgs 'scripts\build_package.py' '--out' 'dist' | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail 'The package was not built.' 'No se generó el paquete.' }
  $pkg = Get-ChildItem -Directory 'dist' -Filter 'dllm-package-*' | Select-Object -First 1
  Pop-Location
  if (-not $pkg) { Fail 'The package was not built.' 'No se generó el paquete.' }

  Say "==> Starting the installer ($($pkg.FullName))" "==> Lanzando el instalador ($($pkg.FullName))"
  Push-Location $pkg.FullName
  & $py[0] @pyArgs 'dllm.pyz' 'setup' @args
  Pop-Location
} catch {
  if ($_.Exception.Message -ne 'DLLM') { Write-Host $_ -ForegroundColor Red }
  Write-Host (T 'Installation did not finish.' 'La instalación no terminó.') -ForegroundColor Red
}
