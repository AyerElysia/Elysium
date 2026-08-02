$ErrorActionPreference = "Stop"

$Python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python 3.12 is not installed at $Python"
}

& $Python -m venv $Venv
& (Join-Path $Venv "Scripts\python.exe") -m pip install --disable-pip-version-check --requirement (Join-Path $Root "requirements.txt")
