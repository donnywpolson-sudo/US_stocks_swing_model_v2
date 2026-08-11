from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TASK_NAME = "USStocksSwingV2-Alpaca-Free-Daily-Capture"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8-sig")


def test_task_installer_binds_exact_wrapper_and_fail_closed_settings() -> None:
    text = _read("install_alpaca_free_daily_capture_task.ps1")
    assert TASK_NAME in text
    assert "run_alpaca_free_daily_capture.ps1" in text
    assert "-WakeToRun" in text
    assert "-StartWhenAvailable" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Hours 4)" in text
    assert "-RestartCount 2" in text
    assert "-RestartInterval (New-TimeSpan -Minutes 5)" in text
    assert "-WorkingDirectory $repositoryRoot" in text
    assert "-DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '04:15'" in text
    assert "-LogonType InteractiveToken -RunLevel Limited" in text


def test_task_action_contains_no_secret_or_trading_arguments() -> None:
    installer = _read("install_alpaca_free_daily_capture_task.ps1")
    wrapper = _read("run_alpaca_free_daily_capture.ps1")
    forbidden = (
        "ALPHAVANTAGE_API_KEY", "Authorization:", "place-order", "submit-order",
        "paper-order", "live-order", "generate-predictions", "train-model",
    )
    assert all(value not in installer for value in forbidden)
    assert all(value not in wrapper for value in forbidden)
    assert "run-daily --execute-network" in wrapper
    assert "api.env" not in wrapper


def test_installer_dry_run_makes_no_network_request_and_requires_safe_api_env() -> None:
    text = _read("install_alpaca_free_daily_capture_task.ps1")
    assert "check-ignore api.env" in text
    assert "ls-files -- api.env" in text
    assert "validate-credentials" in text
    assert "& $wrapper -DryRun" in text
    assert "network_requests_during_install = 0" in text


def test_removal_is_exact_and_preserves_local_evidence() -> None:
    text = _read("remove_alpaca_free_daily_capture_task.ps1")
    assert "Unregister-ScheduledTask -TaskName $taskName" in text
    assert "data_removed = $false" in text
    assert "receipts_removed = $false" in text
    assert "ledgers_removed = $false" in text
    assert "logs_removed = $false" in text


def test_status_is_read_only_and_reports_required_operational_fields() -> None:
    text = _read("show_alpaca_free_daily_capture_status.ps1")
    assert "Get-ScheduledTask" in text
    assert "Get-ScheduledTaskInfo" in text
    assert "prospective_automation status" in text
    assert "acceptance_state" in text
    assert "acceptance_credit" in text
    assert "latest_completed_session" in text
    assert "background_monitor" in text
    assert "structural_pause" in text
    assert "network_requests = 0" in text
