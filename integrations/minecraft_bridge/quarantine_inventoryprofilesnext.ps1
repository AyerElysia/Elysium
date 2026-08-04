param(
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

# A loaded mod set must never be changed in place. Refuse instead of stopping it.
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
    throw "Managed Minecraft is running (PID $processIds); close it manually before changing mods"
}

$mods = Join-Path $resolvedGameRoot "mods"
$quarantine = Join-Path $mods "elysium-disabled\incompatible-inventoryprofilesnext-2.2.5"
$artifacts = @(
    @{
        Name = "InventoryProfilesNext-neoforge-1.21.1-2.2.5.jar"
        Sha256 = "05F1B8660F74F543FBA61D4CE7CF8632D4C69A95B7C15B1A6FD442BE2E397CA6"
    },
    @{
        Name = "libIPN-neoforge-1.21.1-6.6.3.jar"
        Sha256 = "98F061BA69D31764FA10AB7810CA49062633E107A6B261BA3B66DC25BA5FEF7B"
    }
)

foreach ($artifact in $artifacts) {
    $active = Join-Path $mods $artifact.Name
    $archived = Join-Path $quarantine $artifact.Name
    $selected = if (Test-Path -LiteralPath $active -PathType Leaf) {
        $active
    }
    elseif (Test-Path -LiteralPath $archived -PathType Leaf) {
        $archived
    }
    else {
        throw "Expected incompatible artifact is missing: $($artifact.Name)"
    }
    $actualHash = (Get-FileHash -LiteralPath $selected -Algorithm SHA256).Hash
    if ($actualHash -ne $artifact.Sha256) {
        throw "Artifact hash mismatch; refusing to quarantine an unreviewed build: $selected"
    }
}

New-Item -ItemType Directory -Path $quarantine -Force | Out-Null
foreach ($artifact in $artifacts) {
    $active = Join-Path $mods $artifact.Name
    $archived = Join-Path $quarantine $artifact.Name
    if (Test-Path -LiteralPath $active -PathType Leaf) {
        if (Test-Path -LiteralPath $archived) {
            throw "Quarantine target already exists: $archived"
        }
        Move-Item -LiteralPath $active -Destination $archived
    }
}

$stillActive = @(
    $artifacts | Where-Object {
        Test-Path -LiteralPath (Join-Path $mods $_.Name) -PathType Leaf
    }
)
if ($stillActive.Count -ne 0) {
    throw "Quarantine did not remove every selected incompatible artifact"
}
Write-Output "READY|$quarantine"
