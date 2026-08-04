$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Native body is not installed. Run install.ps1 first."
}

$Sidecar = Join-Path $Root "sidecar.py"
$existing = @(
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object {
            ([string]$_.CommandLine).IndexOf(
                "windows_native_body\sidecar.py",
                [System.StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        }
)
if ($existing.Count -gt 0) {
    $owners = $existing | ForEach-Object {
        "PID=$($_.ProcessId) CommandLine=$($_.CommandLine)"
    }
    throw "A Minecraft native body is already running:`n$($owners -join "`n")"
}

& $Python $Sidecar
