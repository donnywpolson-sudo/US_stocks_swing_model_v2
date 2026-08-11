[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$expectedRoot = 'C:\Users\donny\Desktop\US_stocks_swing_model_v2'
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($repositoryRoot, $expectedRoot)) {
    throw 'Repository identity validation failed.'
}

$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The verified Python 3.11 executable is unavailable.'
}

$priorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot 'src'
    Push-Location $repositoryRoot
    try {
        if ($DryRun) {
            & $python -m us_stocks_swing_model_v2.cli.prospective_automation dry-run
        }
        else {
            & $python -m us_stocks_swing_model_v2.cli.prospective_automation run-daily --execute-network
        }
        exit $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:PYTHONPATH = $priorPythonPath
}
