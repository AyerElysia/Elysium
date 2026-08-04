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
    [int]$DiffusionSteps = 8,
    [int]$Seed = 42,
    [double]$BlockTime = 0.24,
    [double]$CrossfadeTime = 0.04,
    [double]$ExtraTimeCE = 2.5,
    [double]$ExtraTime = 0.5,
    [double]$ExtraTimeRight = 0.02,
    [double]$InferenceCfgRate = 0.0,
    [double]$MaxPromptLength = 3.0,
    [double]$SilenceDb = -70.0,
    [double]$OutputGainDb = -3.0
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

if ($BlockTime -lt 0.12 -or $BlockTime -gt 2.0) {
    throw 'BlockTime must be between 0.12 and 2.0 seconds.'
}
if ($CrossfadeTime -lt 0.01 -or $CrossfadeTime -ge $BlockTime) {
    throw 'CrossfadeTime must be positive and shorter than BlockTime.'
}
if ($ExtraTime -lt 0 -or $ExtraTimeCE -lt $ExtraTime) {
    throw 'ExtraTimeCE must not be shorter than ExtraTime.'
}
if ($ExtraTimeCE -gt 10) { throw 'ExtraTimeCE must not exceed 10 seconds.' }
if ($ExtraTimeRight -lt 0 -or $ExtraTimeRight -gt 0.5) {
    throw 'ExtraTimeRight must be between 0 and 0.5 seconds.'
}
if ($DiffusionSteps -lt 1 -or $DiffusionSteps -gt 100) {
    throw 'DiffusionSteps must be between 1 and 100.'
}
if ($InferenceCfgRate -lt 0 -or $InferenceCfgRate -gt 2) {
    throw 'InferenceCfgRate must be between 0 and 2.'
}
if ($MaxPromptLength -lt 0.5 -or $MaxPromptLength -gt 20) {
    throw 'MaxPromptLength must be between 0.5 and 20 seconds.'
}
if ($SilenceDb -lt -120 -or $SilenceDb -gt -20) {
    throw 'SilenceDb must be between -120 and -20 dB.'
}
if ($OutputGainDb -lt -24 -or $OutputGainDb -gt 0) {
    throw 'OutputGainDb must be between -24 and 0 dB.'
}

$expectedAssets = @{
    checkpoint_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $CheckpointPath).Hash.ToLowerInvariant()
    config_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ConfigPath).Hash.ToLowerInvariant()
    reference_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ReferencePath).Hash.ToLowerInvariant()
}
$expectedSettings = @{
    input_sample_rate = 24000.0
    block_time = $BlockTime
    crossfade_time = $CrossfadeTime
    extra_time_ce = $ExtraTimeCE
    extra_time = $ExtraTime
    extra_time_right = $ExtraTimeRight
    diffusion_steps = [double]$DiffusionSteps
    inference_cfg_rate = $InferenceCfgRate
    max_prompt_length = $MaxPromptLength
    silence_db = $SilenceDb
    output_gain_db = $OutputGainDb
    seed = [double]$Seed
}

function Get-SeedVCCompatibilityErrors {
    param([object]$Health)

    $errors = [System.Collections.Generic.List[string]]::new()
    if ($Health.status -ne 'ok') { $errors.Add("status=$($Health.status)") }
    if ([int]$Health.protocol_version -ne 3) {
        $errors.Add("protocol_version=$($Health.protocol_version)")
    }
    if ($Health.profile_id -ne $ProfileId) {
        $errors.Add("profile_id=$($Health.profile_id)")
    }
    foreach ($name in $expectedAssets.Keys) {
        $actualProperty = if ($Health.asset_fingerprints) {
            $Health.asset_fingerprints.PSObject.Properties[$name]
        } else { $null }
        $actual = if ($actualProperty) { [string]$actualProperty.Value } else { '' }
        if ($actual -ne $expectedAssets[$name]) { $errors.Add("asset:$name") }
    }
    foreach ($name in $expectedSettings.Keys) {
        $actualProperty = if ($Health.runtime_settings) {
            $Health.runtime_settings.PSObject.Properties[$name]
        } else { $null }
        if (-not $actualProperty) {
            $errors.Add("setting:$name(missing)")
            continue
        }
        if ([math]::Abs([double]$actualProperty.Value - [double]$expectedSettings[$name]) -gt 0.000001) {
            $errors.Add("setting:$name")
        }
    }
    return $errors
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
    $compatibilityErrors = @(Get-SeedVCCompatibilityErrors -Health $health)
    if ($compatibilityErrors.Count -gt 0) {
        $reason = $compatibilityErrors -join ', '
        throw "Port $Port has a different Seed-VC profile ($reason). Stop it manually, then rerun this launcher; no process was stopped automatically."
    }
    $headers = @{ Authorization = "Bearer $serviceToken" }
    $authorized = Invoke-RestMethod `
        -Uri "http://${wslAddress}:$Port/v1/auth-check" `
        -Method Post -Headers $headers -ContentType 'application/json' `
        -Body '{}' -TimeoutSec 10
    if ($authorized.status -ne 'authorized' -or `
        $authorized.profile_revision -ne $health.profile_revision) {
        throw 'Seed-VC authenticated profile check failed.'
    }
    [pscustomobject]@{
        pid = $existing[0].OwningProcess
        service_url = "http://${wslAddress}:$Port"
        profile_id = $health.profile_id
        profile_revision = $health.profile_revision
        block_time_ms = $health.block_time_ms
        algorithmic_latency_floor_ms = $health.algorithmic_latency_floor_ms
        inference = $health.inference
        model_residency = $health.model_residency
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
    '--seed', $Seed,
    '--block-time', $BlockTime,
    '--crossfade-time', $CrossfadeTime,
    '--extra-time-ce', $ExtraTimeCE,
    '--extra-time', $ExtraTime,
    '--extra-time-right', $ExtraTimeRight,
    '--inference-cfg-rate', $InferenceCfgRate,
    '--max-prompt-length', $MaxPromptLength,
    '--silence-db', $SilenceDb,
    '--output-gain-db', $OutputGainDb
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
$compatibilityErrors = @(Get-SeedVCCompatibilityErrors -Health $health)
if ($compatibilityErrors.Count -gt 0) {
    $reason = $compatibilityErrors -join ', '
    throw "Started Seed-VC returned an incompatible profile ($reason)."
}

[pscustomobject]@{
    pid = $process.Id
    service_url = "http://${wslAddress}:$Port"
    profile_id = $health.profile_id
    profile_revision = $health.profile_revision
    block_time_ms = $health.block_time_ms
    algorithmic_latency_floor_ms = $health.algorithmic_latency_floor_ms
    inference = $health.inference
    model_residency = $health.model_residency
    warmup_ms = $health.warmup_ms
    reused = $false
} | ConvertTo-Json
