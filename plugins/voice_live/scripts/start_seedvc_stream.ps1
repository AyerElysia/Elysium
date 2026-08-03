[CmdletBinding()]
param(
    [string]$PythonPath = $env:ELYSIA_SEEDVC_PYTHON,
    [string]$SeedVCRoot = $env:ELYSIA_SEEDVC_ROOT,
    [string]$CheckpointPath = $env:ELYSIA_SEEDVC_CHECKPOINT,
    [string]$ReferencePath = $env:ELYSIA_SEEDVC_REFERENCE,
    [string]$ConfigPath = $env:ELYSIA_SEEDVC_CONFIG,
    [string]$TokenFile = $env:ELYSIA_SEEDVC_TOKEN_FILE,
    [string]$ProfileId = 'elysia',
    [int]$Port = 17861,
    [int]$DiffusionSteps = 10,
    [int]$Seed = 42
)

$ErrorActionPreference = 'Stop'
$requiredValues = @{
    ELYSIA_SEEDVC_PYTHON = $PythonPath
    ELYSIA_SEEDVC_ROOT = $SeedVCRoot
    ELYSIA_SEEDVC_CHECKPOINT = $CheckpointPath
    ELYSIA_SEEDVC_REFERENCE = $ReferencePath
    ELYSIA_SEEDVC_CONFIG = $ConfigPath
}
foreach ($entry in $requiredValues.GetEnumerator()) {
    if ([string]::IsNullOrWhiteSpace($entry.Value)) {
        throw "Set $($entry.Key) before starting the Seed-VC service."
    }
}

$serviceToken = $env:SEEDVC_STREAM_TOKEN
if ([string]::IsNullOrWhiteSpace($serviceToken)) {
    if ([string]::IsNullOrWhiteSpace($TokenFile)) {
        throw 'Set SEEDVC_STREAM_TOKEN or ELYSIA_SEEDVC_TOKEN_FILE before starting the Seed-VC service.'
    }
    if (-not (Test-Path -LiteralPath $TokenFile -PathType Leaf)) {
        throw "Seed-VC token file is missing: $TokenFile"
    }
    $serviceToken = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($serviceToken)) {
        throw 'Seed-VC token file is empty.'
    }
}

$serviceScript = Join-Path $PSScriptRoot 'seedvc_stream_service.py'
foreach ($requiredFile in @(
    $PythonPath,
    $serviceScript,
    $CheckpointPath,
    $ReferencePath,
    $ConfigPath,
    (Join-Path $SeedVCRoot 'real-time-gui.py')
)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required Seed-VC asset is missing: $requiredFile"
    }
}

$wslAddress = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -like 'vEthernet (WSL*' } |
    Select-Object -First 1 -ExpandProperty IPAddress
if (-not $wslAddress) {
    throw 'Could not discover the Windows address of the WSL virtual network.'
}
$healthUrl = "http://${wslAddress}:$Port/health"
$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
    if ($health.status -ne 'ok' -or $health.profile_id -ne $ProfileId) {
        throw "Port $Port is occupied by an incompatible service."
    }
    $headers = @{ Authorization = "Bearer $serviceToken" }
    $created = $null
    try {
        $created = Invoke-RestMethod -Uri "http://${wslAddress}:$Port/v1/sessions" `
            -Method Post -Headers $headers -ContentType 'application/json' `
            -Body (@{ profile_id = $ProfileId } | ConvertTo-Json -Compress) `
            -TimeoutSec 10
    } finally {
        if ($created.session_id) {
            Invoke-RestMethod `
                -Uri "http://${wslAddress}:$Port/v1/sessions/$($created.session_id)" `
                -Method Delete -Headers $headers -TimeoutSec 10 | Out-Null
        }
    }
    [pscustomobject]@{
        pid = $existing[0].OwningProcess
        service_url = "http://${wslAddress}:$Port"
        profile_id = $health.profile_id
        diffusion_steps = $health.diffusion_steps
        reused = $true
    } | ConvertTo-Json
    exit 0
}

$logRoot = Join-Path $SeedVCRoot 'runtime'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$arguments = @(
    $serviceScript,
    '--seedvc-root', $SeedVCRoot,
    '--checkpoint', $CheckpointPath,
    '--config', $ConfigPath,
    '--reference', $ReferencePath,
    '--profile-id', $ProfileId,
    '--bind', $wslAddress,
    '--port', $Port,
    '--diffusion-steps', $DiffusionSteps,
    '--seed', $Seed
)
if (-not [string]::IsNullOrWhiteSpace($TokenFile)) {
    $arguments += @('--token-file', $TokenFile)
}
$process = Start-Process -FilePath $PythonPath -ArgumentList $arguments `
    -WorkingDirectory $SeedVCRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logRoot 'seedvc-stream.stdout.log') `
    -RedirectStandardError (Join-Path $logRoot 'seedvc-stream.stderr.log')

$deadline = (Get-Date).AddSeconds(120)
do {
    if ($process.HasExited) {
        throw "Seed-VC service exited with code $($process.ExitCode)."
    }
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        if ($health.status -eq 'ok') { break }
    } catch {
        Start-Sleep -Milliseconds 750
    }
} while ((Get-Date) -lt $deadline)
if ($health.status -ne 'ok') {
    throw "Seed-VC health check timed out: $healthUrl"
}

[pscustomobject]@{
    pid = $process.Id
    service_url = "http://${wslAddress}:$Port"
    profile_id = $health.profile_id
    diffusion_steps = $health.diffusion_steps
    warmup_ms = $health.warmup_ms
    reused = $false
} | ConvertTo-Json
