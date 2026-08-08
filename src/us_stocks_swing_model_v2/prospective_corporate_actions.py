"""Fail-closed production effective-event and delisting planning boundary."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .errors import ContractError


POLICY_PATH = "config/prospective_corporate_action_capture_policy.json"
PROJECT = "US_stocks_swing_model_v2"
UNSELECTED = "BACKEND_UNSELECTED"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_policy(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("prospective effective-event/delisting policy is unreadable") from exc
    expected = {
        "schema_version": 2,
        "project": PROJECT,
        "mode": "PROSPECTIVE_EFFECTIVE_EVENT_DELISTING_CAPTURE_BLOCKED",
        "status": UNSELECTED,
        "selected_backend": None,
        "adapter_policy": "config/effective_event_delisting_adapter_policy.json",
        "coverage": {
            "semantics": "EFFECTIVE_EVENT_COMPLETENESS",
            "late_arrival_policy": "revise_outcome_or_abstain_never_backdate",
            "unresolved_rows_remain_in_denominator": True,
            "imputation_or_drop_allowed": False,
        },
        "prohibitions": [
            "alpaca_process_date_evidence_as_complete_effective_event_coverage",
            "capture_planning",
            "release_publication",
            "source_activation",
            "outcome_access",
            "training",
            "evaluation",
        ],
    }
    if value != expected:
        raise ContractError("prospective effective-event/delisting policy differs")
    return value


def _require_selected_backend(repository_root: Path) -> None:
    root = Path(repository_root).resolve(strict=True)
    policy = _load_policy(root)
    if policy["status"] == UNSELECTED or policy["selected_backend"] is None:
        raise ContractError(
            "effective-event/delisting backend is unselected; capture and publication planning are unavailable"
        )


def build_prospective_corporate_action_capture_plan(
    *,
    repository_root: Path,
    accepted_root: Path,
    identity_release_directory: Path,
    bars_release_directory: Path,
    calendar_release_directory: Path,
    symbols: Iterable[str],
    effective_start_session: date,
    effective_end_session: date,
) -> dict[str, object]:
    """Reject production capture planning until a reviewed backend is selected."""
    del (
        accepted_root,
        identity_release_directory,
        bars_release_directory,
        calendar_release_directory,
        symbols,
        effective_start_session,
        effective_end_session,
    )
    _require_selected_backend(repository_root)
    raise AssertionError("configured backend planning is not implemented")


def build_prospective_corporate_action_publication_plan(
    *,
    capture_plan: dict[str, object],
    snapshot_ids: Iterable[str],
    raw_sha256: Iterable[str],
    coverage_id: str,
) -> dict[str, object]:
    """Reject publication planning while production completeness is unavailable."""
    del capture_plan, snapshot_ids, raw_sha256, coverage_id
    _require_selected_backend(_repo_root())
    raise AssertionError("configured backend publication is not implemented")
