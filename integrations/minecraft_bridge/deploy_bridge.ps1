param(
    [string]$SourceJar = (Join-Path $PSScriptRoot "build\libs\elysium_bridge-0.2.0.jar"),
    [string]$GameRoot = "G:\Game\Minecraft\.minecraft"
)

$ErrorActionPreference = "Stop"
$expectedGameRoot = [System.IO.Path]::GetFullPath("G:\Game\Minecraft\.minecraft")
$resolvedGameRoot = [System.IO.Path]::GetFullPath($GameRoot).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)
if (-not $resolvedGameRoot.Equals(
    $expectedGameRoot.TrimEnd('\'),
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "GameRoot must be the managed Minecraft directory: $expectedGameRoot"
}
if (-not (Test-Path -LiteralPath $resolvedGameRoot -PathType Container)) {
    throw "Managed Minecraft directory is missing: $resolvedGameRoot"
}

$resolvedSource = [System.IO.Path]::GetFullPath($SourceJar)
$buildRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "build\libs")
).TrimEnd('\') + '\'
if (-not $resolvedSource.StartsWith(
    $buildRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "SourceJar must be built under $buildRoot"
}
if (-not (Test-Path -LiteralPath $resolvedSource -PathType Leaf)) {
    throw "Built bridge artifact is missing: $resolvedSource"
}

# Deploying into a loaded mod set is ambiguous.  Refuse instead of stopping the game.
$managedProcesses = Get-CimInstance Win32_Process -Filter "Name = 'java.exe' OR Name = 'javaw.exe'" |
    Where-Object {
        $line = [string]$_.CommandLine
        $line.Contains("BootstrapLauncher") -and
        $line.IndexOf(
            $resolvedGameRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -ge 0
    }
if ($managedProcesses) {
    $processIds = ($managedProcesses.ProcessId | Sort-Object) -join ', '
    throw "Managed Minecraft is running (PID $processIds); close it manually before deployment"
}

$lockPath = Join-Path $PSScriptRoot "bridge-artifact-lock.json"
$lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
$actualHash = (Get-FileHash -LiteralPath $resolvedSource -Algorithm SHA256).Hash
if ($actualHash -ne [string]$lock.sha256) {
    throw "Bridge artifact hash mismatch: expected $($lock.sha256), received $actualHash"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($resolvedSource)
try {
    $metadataEntry = $archive.GetEntry("META-INF/neoforge.mods.toml")
    if ($null -eq $metadataEntry) {
        throw "Bridge artifact has no NeoForge metadata"
    }
    $metadataStream = $metadataEntry.Open()
    $reader = [System.IO.StreamReader]::new($metadataStream)
    try {
        $metadata = $reader.ReadToEnd()
    }
    finally {
        $reader.Dispose()
    }
}
finally {
    $archive.Dispose()
}
if ($metadata -notmatch ('version="' + [regex]::Escape([string]$lock.bridge_version) + '"')) {
    throw "Bridge metadata version does not match the artifact lock"
}

$mods = Join-Path $resolvedGameRoot "mods"
if (-not (Test-Path -LiteralPath $mods -PathType Container)) {
    throw "Minecraft mods directory is missing: $mods"
}
$baritoneCandidates = @(
    Get-ChildItem -LiteralPath $mods -File -Filter "baritone*.jar"
)
if ($baritoneCandidates.Count -ne 1 -or $baritoneCandidates[0].Name -ne [string]$lock.baritone.file_name) {
    throw "Exactly one pinned official Baritone NeoForge artifact must be selected"
}
$baritoneSha1 = (Get-FileHash -LiteralPath $baritoneCandidates[0].FullName -Algorithm SHA1).Hash
$baritoneSha256 = (Get-FileHash -LiteralPath $baritoneCandidates[0].FullName -Algorithm SHA256).Hash
if (
    $baritoneSha1 -ne [string]$lock.baritone.official_sha1 -or
    $baritoneSha256 -ne [string]$lock.baritone.sha256
) {
    throw "Selected Baritone artifact does not match the official pinned release"
}
$destination = Join-Path $mods ([string]$lock.file_name)
$legacy = @(
    Get-ChildItem -LiteralPath $mods -File -Filter "elysium_bridge-*.jar" |
        Where-Object { -not $_.FullName.Equals(
            $destination,
            [System.StringComparison]::OrdinalIgnoreCase
        ) }
)
$destinationNeedsInstall = $true
if (Test-Path -LiteralPath $destination -PathType Leaf) {
    $installedHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    $destinationNeedsInstall = $installedHash -ne $actualHash
}

if ($legacy.Count -gt 0 -or $destinationNeedsInstall) {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $backupDirectory = Join-Path $mods "elysium-disabled\$stamp"
    New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
    foreach ($jar in $legacy) {
        Move-Item -LiteralPath $jar.FullName -Destination $backupDirectory
    }
    if ((Test-Path -LiteralPath $destination) -and $destinationNeedsInstall) {
        Move-Item -LiteralPath $destination -Destination $backupDirectory
    }
}

if ($destinationNeedsInstall) {
    $temporary = "$destination.elysium.tmp"
    Copy-Item -LiteralPath $resolvedSource -Destination $temporary
    $copiedHash = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash
    if ($copiedHash -ne $actualHash) {
        Remove-Item -LiteralPath $temporary
        throw "Copied bridge artifact failed its hash check"
    }
    Move-Item -LiteralPath $temporary -Destination $destination -Force
}

$remaining = @(Get-ChildItem -LiteralPath $mods -File -Filter "elysium_bridge-*.jar")
if ($remaining.Count -ne 1 -or -not $remaining[0].FullName.Equals(
    $destination,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Deployment did not leave exactly one selected Elysium bridge mod"
}
Write-Output "READY|$destination|$actualHash"
