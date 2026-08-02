[CmdletBinding()]
param(
    [string]$RuntimeRoot = $env:ELYSIA_VOICE_MODEL_ROOT,
    [int]$Port = 9060,
    [int]$ContextSize = 8192
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    throw 'Set ELYSIA_VOICE_MODEL_ROOT or pass -RuntimeRoot.'
}
$runtime = [System.IO.Path]::GetFullPath($RuntimeRoot)
$binaryDir = Join-Path $runtime 'llama.cpp-omni\build-windows-cuda128\bin'
$binary = Join-Path $binaryDir 'llama-omni-server.exe'
$toolkitBin = Join-Path $runtime 'cuda-12.8-toolkit\bin'
$model = Join-Path $runtime 'MiniCPM-o-4_5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf'
foreach ($required in @($binary, $model, (Join-Path $toolkitBin 'cudart64_12.dll'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required local Omni asset is missing: $required"
    }
}

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $owner = Get-Process -Id $existing[0].OwningProcess -ErrorAction Stop
    if ($owner.Path -ne $binary) {
        throw "Port $Port is already owned by $($owner.Path)."
    }
    $process = $owner
} else {
    $logRoot = Join-Path $runtime 'runtime'
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    $env:PATH = "$toolkitBin;$binaryDir;$env:PATH"
    $arguments = @(
        '--host', '0.0.0.0', '--port', $Port,
        '--model', $model, '-ngl', '99',
        '--ctx-size', $ContextSize,
        '--repeat-penalty', '1.05', '--temp', '0.7'
    )
    $process = Start-Process -FilePath $binary -ArgumentList $arguments `
        -WorkingDirectory $binaryDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot 'minicpm-omni.stdout.log') `
        -RedirectStandardError (Join-Path $logRoot 'minicpm-omni.stderr.log')
}

$wslAddress = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -like 'vEthernet (WSL*' } |
    Select-Object -First 1 -ExpandProperty IPAddress
if (-not $wslAddress) {
    throw 'Could not discover the Windows address of the WSL virtual network.'
}
$healthUrl = "http://${wslAddress}:$Port/health"
$deadline = (Get-Date).AddSeconds(30)
do {
    if ($process.HasExited) {
        throw "llama-omni-server exited with code $($process.ExitCode)."
    }
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.status -eq 'ok') { break }
    } catch {
        Start-Sleep -Milliseconds 500
    }
} while ((Get-Date) -lt $deadline)
if ($health.status -ne 'ok') {
    throw "Local Omni health check timed out: $healthUrl"
}

[pscustomobject]@{
    pid = $process.Id
    health_url = $healthUrl
    wsl_websocket_url = "ws://${wslAddress}:$Port/backend"
    model = $model
    status = $health.status
} | ConvertTo-Json
