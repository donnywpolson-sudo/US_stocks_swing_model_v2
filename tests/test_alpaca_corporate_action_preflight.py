from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from us_stocks_swing_model_v2.alpaca_corporate_action_preflight import build_corporate_action_preflight
from us_stocks_swing_model_v2 import alpaca_corporate_action_preflight as preflight_module
from us_stocks_swing_model_v2.cli import plan_alpaca_corporate_actions as cli_module
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest


REPO = Path(__file__).resolve().parents[1]


def _release(tmp_path: Path) -> tuple[Path, Path]:
    stage = tmp_path / "stage"; stage.mkdir(); (stage / "bars").mkdir()
    (stage / "bars" / "year=2016.parquet").write_bytes(b"synthetic")
    manifest = build_manifest(stage, ("bars/year=2016.parquet",), project="US_stocks_swing_model_v2", dataset="alpaca_historical_daily_bars", source_epoch="synthetic", role="legacy_discovery_only", quality_state="LEGACY_CAVEATED", created_at="2026-07-31T20:00:00Z", row_count=1, event_start="2016-01-04", event_end="2016-01-05", schema_fingerprint="a" * 64, code_hash="b" * 64, config_hash="c" * 64, environment_hash="d" * 64)
    accepted = (tmp_path / "accepted").resolve()
    return AtomicReleasePublisher(accepted).publish(stage, manifest), accepted


def test_preflight_is_no_network_and_preserves_outcome_blockers(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    plan = build_corporate_action_preflight(release_directory=release, accepted_root=accepted, start=date(2016, 1, 4), end=date(2016, 1, 5), symbols=("AAPL",), max_pages=1, created_at="2026-07-31T20:00:00Z", repo_root=REPO)
    assert plan["request"]["url"].startswith("https://data.alpaca.markets/v1/corporate-actions?")
    assert plan["request"]["request_count"] == 1
    assert plan["request"]["continuation_token_disposition"] == "STOP_WITHOUT_SECOND_REQUEST"
    assert plan["request"]["network_request_plan"]["max_pages"] == 1
    assert plan["request"]["network_request_plan"]["pagination_parameter"] == "page_token"
    assert plan["outcome_boundary"]["outcomes_may_compute"] is False
    assert all(value is False for value in plan["authorities"].values())


def test_preflight_rejects_noncanonical_symbols(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    with pytest.raises(ContractError):
        build_corporate_action_preflight(release_directory=release, accepted_root=accepted, start=date(2016, 1, 4), end=date(2016, 1, 5), symbols=("aapl",), max_pages=1, created_at="2026-07-31T20:00:00Z", repo_root=REPO)


def test_execution_rejects_wrong_plan_before_transport(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    plan = build_corporate_action_preflight(release_directory=release, accepted_root=accepted, start=date(2016, 1, 4), end=date(2016, 1, 5), symbols=("AAPL",), max_pages=1, created_at="2026-07-31T20:00:00Z", repo_root=REPO)
    with pytest.raises(ContractError, match="approved corporate-action preflight plan differs"):
        preflight_module.execute_corporate_action_preflight(
            plan=plan,
            approved_plan_id="0" * 64,
            api_key_id="synthetic-key",
            api_secret_key="synthetic-secret",
            clock=object(),
            repo_root=REPO,
        )


def test_execution_rejects_tampered_plan_identity(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    plan = build_corporate_action_preflight(release_directory=release, accepted_root=accepted, start=date(2016, 1, 4), end=date(2016, 1, 5), symbols=("AAPL",), max_pages=1, created_at="2026-07-31T20:00:00Z", repo_root=REPO)
    plan["request"]["max_pages"] = 2
    with pytest.raises(ContractError, match="preflight plan identity differs"):
        preflight_module.execute_corporate_action_preflight(
            plan=plan,
            approved_plan_id=plan["plan_id"],
            api_key_id="synthetic-key",
            api_secret_key="synthetic-secret",
            clock=object(),
            repo_root=REPO,
        )


def test_execution_uses_one_bound_page_and_verifies_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release, accepted = _release(tmp_path)
    plan = build_corporate_action_preflight(release_directory=release, accepted_root=accepted, start=date(2016, 1, 4), end=date(2016, 1, 5), symbols=("AAPL",), max_pages=1, created_at="2026-07-31T20:00:00Z", repo_root=REPO)
    page = SimpleNamespace(snapshot_id="a" * 64, raw_sha256="b" * 64)
    observed: dict[str, object] = {}

    monkeypatch.setattr(preflight_module, "start_local_network_execution", lambda *args, **kwargs: object())
    def fetch(request, **kwargs):
        observed["url"] = request.url()
        observed["max_pages"] = kwargs["max_pages"]
        observed["timeout_seconds"] = kwargs["timeout_seconds"]
        return (page,)
    monkeypatch.setattr(preflight_module, "guarded_fetch_corporate_action_pages", fetch)
    monkeypatch.setattr(
        preflight_module,
        "parse_landed_corporate_actions",
        lambda request, pages: SimpleNamespace(actions=(object(), object()), coverage=SimpleNamespace(coverage_id="c" * 64)),
    )

    result = preflight_module.execute_corporate_action_preflight(
        plan=plan,
        approved_plan_id=plan["plan_id"],
        api_key_id="synthetic-key",
        api_secret_key="synthetic-secret",
        clock=object(),
        repo_root=REPO,
    )
    assert observed == {"url": plan["request"]["url"], "max_pages": 1, "timeout_seconds": 30}
    assert result["network_calls"] == 1
    assert result["verified"] is True
    assert result["published"] is False


def test_cli_executes_only_the_matching_approved_plan(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    plan = {"plan_id": "d" * 64}
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "build_corporate_action_preflight", lambda **kwargs: plan)
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    def execute(**kwargs):
        observed.update(kwargs)
        return {"verified": True, "network_calls": 1}
    monkeypatch.setattr(cli_module, "execute_corporate_action_preflight", execute)

    assert cli_module.main([
        "--release-directory", "synthetic-release", "--start", "2016-01-04",
        "--end", "2016-01-05", "--symbols", "AAPL", "--created-at",
        "2026-07-31T20:00:00Z", "--execute-network", "--approved-plan-id", "d" * 64,
    ]) == 0
    assert observed["plan"] is plan
    assert observed["approved_plan_id"] == "d" * 64
    assert '"mode": "CAPTURED_AND_VERIFIED_NOT_PUBLISHED"' in capsys.readouterr().out
