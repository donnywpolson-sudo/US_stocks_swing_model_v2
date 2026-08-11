[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$taskName = 'USStocksSwingV2-Alpaca-Free-Daily-Capture'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $PSCmdlet.ShouldProcess($taskName, 'Remove exact scheduled task')) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
[ordered]@{
    task_name = $taskName
    removed = [bool]$existing
    data_removed = $false
    receipts_removed = $false
    ledgers_removed = $false
    logs_removed = $false
} | ConvertTo-Json
