param(
    [Parameter(Mandatory=$true)]
    [string]$LaunchScriptPath,
    [Parameter(Mandatory=$true)]
    [string]$WorkingDirectory
)

$ErrorActionPreference = "Stop"
$allowedRoot = [System.IO.Path]::GetFullPath("G:\Game\Minecraft\PCL").TrimEnd('\')
$resolvedScript = [System.IO.Path]::GetFullPath($LaunchScriptPath)
$resolvedWorkingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory).TrimEnd('\')
if (-not $resolvedWorkingDirectory.Equals(
    $allowedRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Working directory is not the managed PCL directory"
}
$rootPrefix = $allowedRoot + '\'
if (-not $resolvedScript.StartsWith(
    $rootPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Launch script is outside the managed PCL directory"
}
if (-not (Test-Path -LiteralPath $resolvedScript -PathType Leaf)) {
    throw "Launch script is missing"
}

$process = Start-Process `
    -FilePath $resolvedScript `
    -WorkingDirectory $resolvedWorkingDirectory `
    -WindowStyle Hidden `
    -PassThru
Write-Output "DISPATCHED|$($process.Id)"
