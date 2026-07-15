from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.calendar import PinnedSessionCalendar
from us_stocks_swing_model_v2.corporate_actions import (
    ActionType,
    BitemporalActionLedger,
    CorporateAction,
)
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.outcomes import DailyBar, build_outcome
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest
from us_stocks_swing_model_v2.schemas import OutcomeStatus


SESSIONS = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13"]
AS_OF = datetime(2026, 7, 14, tzinfo=timezone.utc)
BAR_RELEASE_ID = "e" * 64


def _action_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="calendar-actions-ledger",
        scope="SYNTHETIC_CORPORATE_ACTION_LEDGER",
    )


def _action_ledger(actions=()) -> BitemporalActionLedger:
    return BitemporalActionLedger(actions, synthetic_permit=_action_permit())


def _calendar() -> PinnedSessionCalendar:
    permit = SyntheticOnlyPermit.create(
        fixture_id="calendar-actions-outcomes",
        scope="SYNTHETIC_SESSION_CALENDAR",
    )
    return PinnedSessionCalendar.from_iso_dates(
        permit.permit_id,
        SESSIONS,
        synthetic_permit=permit,
    )


def _bars(exit_close: float = 55.0) -> dict[date, DailyBar]:
    result = {}
    for value in SESSIONS[1:]:
        session = date.fromisoformat(value)
        result[session] = DailyBar("asset-1", session, 100.0, exit_close, AS_OF)
    return result


def _action(action_type: ActionType, *, revision: int = 1, ratio: float | None = None, received: datetime = AS_OF) -> CorporateAction:
    permit = _action_permit()
    return CorporateAction(
        action_id="action-1",
        asset_id="asset-1",
        action_type=action_type,
        effective_session=date(2026, 7, 10),
        announced_at=None,
        received_at=received,
        revision=revision,
        source_snapshot_id="a" * 64,
        source_release_id=permit.permit_id,
        source_epoch="SYNTHETIC_ONLY",
        raw_row_sha256="b" * 64,
        ratio_new_for_old=ratio,
    )


def test_split_normalized_next_open_to_fifth_close_uses_pinned_sessions() -> None:
    outcome = build_outcome(
        prediction_id="1" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=_bars(),
        bar_release_id=BAR_RELEASE_ID,
        actions=_action_ledger([_action(ActionType.SPLIT, ratio=2.0)]),
        action_view_as_of=AS_OF,
        source_epoch="fixture_epoch",
    )
    assert outcome.status is OutcomeStatus.MATURED
    assert outcome.entry_session == date(2026, 7, 7)
    assert outcome.exit_session == date(2026, 7, 13)
    assert outcome.split_normalized_price_return == pytest.approx(0.10)


def test_corporate_action_revisions_are_bitemporal() -> None:
    early = datetime(2026, 7, 11, tzinfo=timezone.utc)
    late = datetime(2026, 7, 14, tzinfo=timezone.utc)
    ledger = _action_ledger(
        [
            _action(ActionType.SPLIT, revision=1, ratio=2.0, received=early),
            _action(ActionType.SPLIT, revision=2, ratio=3.0, received=late),
        ],
    )
    assert ledger.visible_as_of("asset-1", early)[0].ratio_new_for_old == 2.0
    assert ledger.visible_as_of("asset-1", late)[0].ratio_new_for_old == 3.0


def test_corporate_action_ledger_requires_verified_release_or_explicit_synthetic_scope(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    action_payload = {
        "schema_version": 1,
        "actions": [{
            "action_id": "verified-action",
            "asset_id": "asset-1",
            "action_type": "SPLIT",
            "effective_session": "2026-07-10",
            "announced_at": None,
            "received_at": "2026-07-14T00:00:00Z",
            "revision": 1,
            "source_snapshot_id": "a" * 64,
            "raw_row_sha256": "b" * 64,
            "ratio_new_for_old": 2.0,
            "voided": False,
        }],
    }
    (stage / "corporate_actions.json").write_text(
        json.dumps(action_payload, sort_keys=True), encoding="utf-8"
    )
    manifest = build_manifest(
        stage,
        ["corporate_actions.json"],
        project="US_stocks_swing_model_v2",
        dataset="corporate_actions",
        source_epoch="alpaca_corporate_actions_v1",
        role="prospective_as_received",
        quality_state="PASS",
        created_at="2026-07-15T12:05:00Z",
        row_count=1,
        event_start="2026-07-10",
        event_end="2026-07-10",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    release = AtomicReleasePublisher(tmp_path / "accepted").publish(stage, manifest)
    ledger = BitemporalActionLedger(
        verified_release_directory=release,
        accepted_release_root=tmp_path / "accepted",
    )
    assert ledger.trust_eligible is True
    assert ledger.release_id == manifest.release_id

    with pytest.raises(ContractError, match="must come only from release payload"):
        BitemporalActionLedger(
            [replace(ledger.visible_as_of("asset-1", AS_OF)[0], source_release_id="f" * 64)],
            verified_release_directory=release,
            accepted_release_root=tmp_path / "accepted",
        )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", OutcomeStatus.MISSING_SOURCE),
        ("halted", OutcomeStatus.HALTED),
        ("delisted", OutcomeStatus.DELISTED),
        ("merger", OutcomeStatus.ACTION_UNRESOLVED),
    ],
)
def test_unresolved_paths_are_retained_not_dropped(mutation: str, expected: OutcomeStatus) -> None:
    bars = _bars(exit_close=110.0)
    actions = _action_ledger()
    exit_session = date(2026, 7, 13)
    if mutation == "missing":
        del bars[exit_session]
    elif mutation == "halted":
        bars[exit_session] = DailyBar("asset-1", exit_session, None, None, AS_OF, halted=True)
    elif mutation == "delisted":
        middle = date(2026, 7, 10)
        bars[middle] = DailyBar("asset-1", middle, None, None, AS_OF, delisted=True)
    else:
        actions.append(_action(ActionType.MERGER))
    outcome = build_outcome(
        prediction_id="2" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=bars,
        bar_release_id=BAR_RELEASE_ID,
        actions=actions,
        action_view_as_of=AS_OF,
        source_epoch="fixture_epoch",
    )
    assert outcome.status is expected
    assert outcome.split_normalized_price_return is None
    assert outcome.reason
