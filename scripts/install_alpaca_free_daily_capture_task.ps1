[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$taskName = 'USStocksSwingV2-Alpaca-Free-Daily-Capture'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$expectedRoot = 'C:\Users\donny\Desktop\US_stocks_swing_model_v2'
$wrapper = Join-Path $repositoryRoot 'scripts\run_alpaca_free_daily_capture.ps1'
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'

if (-not [StringComparer]::OrdinalIgnoreCase.Equals($repositoryRoot, $expectedRoot)) {
    throw 'Repository identity validation failed.'
}
if ((git -C $repositoryRoot branch --show-current) -ne 'alpaca-free-bounded-long-short') {
    throw 'The automation task may be installed only from the implementation branch.'
}
if ((git -C $repositoryRoot merge-base c29e244174940f76babf75bcf91bbd11ca470c46 HEAD) -ne 'c29e244174940f76babf75bcf91bbd11ca470c46') {
    throw 'The implementation branch ancestry differs from the frozen reference.'
}
if (git -C $repositoryRoot status --short) {
    throw 'Task installation requires a clean worktree.'
}
if ((git -C $repositoryRoot check-ignore api.env) -ne 'api.env') {
    throw 'api.env is not safely ignored.'
}
if (git -C $repositoryRoot ls-files -- api.env) {
    throw 'api.env is tracked.'
}
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
    throw 'The tracked master wrapper is unavailable.'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The verified Python 3.11 executable is unavailable.'
}

$priorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot 'src'
    $credentialOutput = & $python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded validate-credentials 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Canonical credential-presence validation failed.' }
    $credentialState = $credentialOutput | ConvertFrom-Json
    if ($credentialState.presence.APCA_API_KEY_ID -ne $true -or
        $credentialState.presence.APCA_API_SECRET_KEY -ne $true -or
        $credentialState.presence.ALPHA_VANTAGE_API_KEY -ne $true) {
        throw 'One or more canonical credential variables are unavailable.'
    }
    & $python -m us_stocks_swing_model_v2.cli.prospective_automation validate-policy | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Automation policy validation failed.' }
    & $wrapper -DryRun | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'No-network automation dry-run failed.' }
}
finally {
    $env:PYTHONPATH = $priorPythonPath
}

$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$quotedWrapper = '"' + $wrapper + '"'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + $quotedWrapper
$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $repositoryRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '04:15'
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principalId = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal -UserId $principalId -LogonType Interactive -RunLevel Limited
$definition = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Fail-closed ALPACA_FREE_BOUNDED_V1 prospective capture. No trading or order action.'

if (-not $DryRun) {
    Register-ScheduledTask -TaskName $taskName -InputObject $definition -Force | Out-Null
}

$registered = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$info = if ($registered) { Get-ScheduledTaskInfo -TaskName $taskName } else { $null }
[ordered]@{
    state = if ($DryRun) { 'DRY_RUN_PASS' } else { 'INSTALLED' }
    task_name = $taskName
    registered = [bool]$registered
    enabled = if ($registered) { $registered.State -ne 'Disabled' } else { $false }
    principal = $principalId
    logon_type = 'Interactive'
    trigger = 'WEEKDAYS_04:15_AMERICA_LOS_ANGELES_WITH_XNYS_RUNTIME_GATE'
    action = $wrapper
    working_directory = $repositoryRoot
    wake_to_run = $true
    start_when_available = $true
    multiple_instances = 'IgnoreNew'
    execution_time_limit_minutes = 240
    restart_count = 2
    restart_interval_minutes = 5
    next_run_time = if ($info) { $info.NextRunTime.ToString('o') } else { $null }
    credentials_in_task_definition = $false
    network_requests_during_install = 0
} | ConvertTo-Json -Depth 4
