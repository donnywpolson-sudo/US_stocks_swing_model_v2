[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskName = 'USStocksSwingV2-Alpaca-Free-Daily-Capture'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $taskName } else { $null }
$priorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot 'src'
    $automation = & $python -m us_stocks_swing_model_v2.cli.prospective_automation status | ConvertFrom-Json
}
finally {
    $env:PYTHONPATH = $priorPythonPath
}
[ordered]@{
    task_name = $taskName
    installed = [bool]$task
    enabled = if ($task) { $task.State -ne 'Disabled' } else { $false }
    principal = if ($task) { $task.Principal.UserId } else { $null }
    last_run_time = if ($taskInfo) { $taskInfo.LastRunTime.ToString('o') } else { $null }
    last_result_code = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    next_run_time = if ($taskInfo) { $taskInfo.NextRunTime.ToString('o') } else { $null }
    acceptance_state = $automation.acceptance.state
    acceptance_credit = $automation.acceptance.completed_consecutive_sessions
    latest_completed_session = $automation.acceptance.latest_completed_session
    background_monitor = $automation.background_monitor
    structural_pause = $automation.acceptance.state -eq 'AUTOMATION_PAUSED_STRUCTURAL_FAILURE'
    network_requests = 0
} | ConvertTo-Json -Depth 8
