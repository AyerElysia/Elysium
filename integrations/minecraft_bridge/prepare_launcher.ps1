param(
    [string]$LaunchScriptPath = "G:\Game\Minecraft\PCL\LaunchElysia.bat",
    [string]$WorldName = "Elysian Realm",
    [string]$AllowedRoot = "G:\Game\Minecraft\PCL"
)

$ErrorActionPreference = "Stop"

$resolved = [System.IO.Path]::GetFullPath($LaunchScriptPath)
$resolvedAllowedRoot = [System.IO.Path]::GetFullPath($AllowedRoot)
$rootWithSeparator = $resolvedAllowedRoot.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolved.StartsWith($rootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Launch script must remain inside $resolvedAllowedRoot"
}
if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
    throw "Minecraft launch script is missing: $resolved"
}
if ([string]::IsNullOrWhiteSpace($WorldName) -or $WorldName.Contains('"')) {
    throw "WorldName must be non-empty and must not contain a double quote"
}

$content = [System.IO.File]::ReadAllText($resolved, [System.Text.Encoding]::UTF8)
$escapedWorld = [System.Text.RegularExpressions.Regex]::Escape($WorldName)
$exactPattern = "--quickPlaySingleplayer\s+`"$escapedWorld`""
if ($content -match $exactPattern) {
    Write-Output "READY|$resolved|$WorldName"
    exit 0
}

$foreignQuickPlay = [System.Text.RegularExpressions.Regex]::Match(
    $content,
    '--quickPlaySingleplayer(?:=|\s+)(?:"[^"]+"|''[^'']+''|\S+)',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
)
if ($foreignQuickPlay.Success) {
    throw "Launch script already targets a different quick-play world"
}

$launchTargetMatches = [System.Text.RegularExpressions.Regex]::Matches(
    $content,
    '--launchTarget\s+forgeclient',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
)
if ($launchTargetMatches.Count -ne 1) {
    throw "Expected exactly one '--launchTarget forgeclient' marker"
}

$backup = "$resolved.pre-elysium-quickplay.bak"
if (-not (Test-Path -LiteralPath $backup)) {
    Copy-Item -LiteralPath $resolved -Destination $backup
}
$replacement = "--launchTarget forgeclient --quickPlaySingleplayer `"$WorldName`""
$updated = [System.Text.RegularExpressions.Regex]::Replace(
    $content,
    '--launchTarget\s+forgeclient',
    $replacement,
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
)
$temporary = "$resolved.elysium.tmp"
[System.IO.File]::WriteAllText(
    $temporary,
    $updated,
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $temporary -Destination $resolved -Force
Write-Output "UPDATED|$resolved|$backup|$WorldName"
