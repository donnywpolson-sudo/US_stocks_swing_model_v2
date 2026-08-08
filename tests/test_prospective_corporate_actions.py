from datetime import date
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.cli.plan_prospective_corporate_actions import main as plan_main
from us_stocks_swing_model_v2 import prospective_corporate_actions as planning
from us_stocks_swing_model_v2.prospective_corporate_actions import (
    build_prospective_corporate_action_capture_plan,
    build_prospective_corporate_action_publication_plan,
)


REPO = Path(__file__).parents[1]


def test_capture_planning_fails_before_inputs_can_claim_completeness() -> None:
    with pytest.raises(ContractError, match="backend is unselected"):
        build_prospective_corporate_action_capture_plan(
            repository_root=REPO,
            accepted_root=REPO / "data/vault/accepted",
            identity_release_directory=REPO / "not-an-accepted-identity",
            bars_release_directory=REPO / "not-accepted-bars",
            calendar_release_directory=REPO / "not-an-accepted-calendar",
            symbols=("AAPL", "SPY"),
            effective_start_session=date(2026, 7, 27),
            effective_end_session=date(2026, 8, 10),
        )


def test_forged_capture_cannot_reach_publication_planning() -> None:
    with pytest.raises(ContractError, match="backend is unselected"):
        build_prospective_corporate_action_publication_plan(
            capture_plan={"capture_plan_id": "a" * 64},
            snapshot_ids=("b" * 64,),
            raw_sha256=("c" * 64,),
            coverage_id="d" * 64,
        )


def test_policy_never_names_alpaca_as_completeness_backend() -> None:
    text = (REPO / "config/prospective_corporate_action_capture_policy.json").read_text(
        encoding="utf-8"
    )
    assert '"selected_backend": null' in text
    assert '"status": "BACKEND_UNSELECTED"' in text
    assert '"source": "alpaca_corporate_actions"' not in text


def test_public_cli_route_cannot_emit_a_plan() -> None:
    with pytest.raises(ContractError, match="backend is unselected"):
        plan_main(
            [
                "--identity-release", "ignored-identity",
                "--bars-release", "ignored-bars",
                "--calendar-release", "ignored-calendar",
                "--symbols", "AAPL,SPY",
                "--effective-start", "2026-07-27",
                "--effective-end", "2026-08-10",
            ]
        )


def test_backend_fields_cannot_be_flipped_without_full_policy_review(tmp_path: Path) -> None:
    policy = json.loads(
        (REPO / "config/prospective_corporate_action_capture_policy.json").read_text(
            encoding="utf-8"
        )
    )
    policy["status"] = "BACKEND_SELECTED"
    policy["selected_backend"] = {"provider_id": "unreviewed"}
    config = tmp_path / "config"
    config.mkdir()
    (config / "prospective_corporate_action_capture_policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    with pytest.raises(ContractError, match="policy differs"):
        planning._require_selected_backend(tmp_path)
