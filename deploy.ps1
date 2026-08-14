[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $DeploymentArguments
)

$ErrorActionPreference = "Stop"
$DeploymentRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DeploymentScript = Join-Path $DeploymentRoot "scripts/deployment.py"

$PythonCommand = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $PythonCommand) {
    & $PythonCommand.Source -3 $DeploymentScript @DeploymentArguments
    exit $LASTEXITCODE
}

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $PythonCommand) {
    Write-Error "部署失败: 未找到 Python 3.11+"
    exit 2
}

& $PythonCommand.Source $DeploymentScript @DeploymentArguments
exit $LASTEXITCODE
