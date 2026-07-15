from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.ledger import HashChainLedger, OutcomeLedger, PredictionLedger
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest
from us_stocks_swing_model_v2.schemas import OutcomeRow, OutcomeStatus, SecurityType, UnderlyingPrediction
from us_stocks_swing_model_v2.source_selection import select_explicit_release


def _clock(at: datetime) -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        at,
        permit=SyntheticOnlyPermit.create(
            fixture_id=f"recovery-{at.isoformat()}",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def test_source_selection_uses_one_exact_accepted_manifest_and_role(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "bars.bin").write_bytes(b"bars")
    manifest = build_manifest(
        stage,
        ["bars.bin"],
        project="US_stocks_swing_model_v2",
        dataset="qualification_bars",
        source_epoch="alpaca_qualification_v1",
        role="qualification_evidence_only",
        quality_state="FAIL",
        created_at="2026-07-15T00:00:00Z",
        row_count=1,
        event_start="2026-07-14",
        event_end="2026-07-14",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    release = AtomicReleasePublisher(tmp_path / "releases").publish(stage, manifest)
    selected = select_explicit_release(
        release,
        expected_dataset="qualification_bars",
        expected_release_id=manifest.release_id,
        expected_project="US_stocks_swing_model_v2",
        accepted_release_root=tmp_path / "releases",
        allowed_epoch_roles={"alpaca_qualification_v1": {"qualification_evidence_only"}},
        allowed_quality_states={"FAIL"},
    )
    assert selected.release_id == manifest.release_id
    with pytest.raises(ContractError, match="quality"):
        select_explicit_release(
            release,
            expected_dataset="qualification_bars",
            expected_release_id=manifest.release_id,
            expected_project="US_stocks_swing_model_v2",
            accepted_release_root=tmp_path / "releases",
            allowed_epoch_roles={"alpaca_qualification_v1": {"qualification_evidence_only"}},
            allowed_quality_states={"QUALIFICATION_EVIDENCE"},
        )
    with pytest.raises(ContractError, match="exact accepted"):
        select_explicit_release(
            release,
            expected_dataset="qualification_bars",
            expected_release_id="0" * 64,
            expected_project="US_stocks_swing_model_v2",
            accepted_release_root=tmp_path / "releases",
            allowed_epoch_roles={"alpaca_qualification_v1": {"qualification_evidence_only"}},
            allowed_quality_states={"FAIL"},
        )
    with pytest.raises(ContractError, match="role"):
        select_explicit_release(
            release,
            expected_dataset="qualification_bars",
            expected_release_id=manifest.release_id,
            expected_project="US_stocks_swing_model_v2",
            accepted_release_root=tmp_path / "releases",
            allowed_epoch_roles={"alpaca_qualification_v1": {"active_historical"}},
            allowed_quality_states={"FAIL"},
        )
    pending = tmp_path / "releases" / ".staging" / "fake"
    pending.mkdir(parents=True)
    with pytest.raises(ContractError, match="pending/staging"):
        select_explicit_release(
            pending,
            expected_dataset="qualification_bars",
            expected_release_id=manifest.release_id,
            expected_project="US_stocks_swing_model_v2",
            accepted_release_root=tmp_path / "releases",
            allowed_epoch_roles={"alpaca_qualification_v1": {"qualification_evidence_only"}},
            allowed_quality_states={"FAIL"},
        )


def test_hash_ledger_recovers_precommit_journal_atomically(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=_clock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
    )
    first = ledger.append({"id": 1})
    unsigned = {
        "sequence": 1,
        "previous_hash": first["record_hash"],
        "record_type": "fixture_v1",
        "recorded_at": "2026-07-15T00:01:00Z",
        "time_authority": "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
        "synthetic_clock_permit_id": _clock(
            datetime(2026, 7, 15, 0, 1, tzinfo=timezone.utc)
        ).synthetic_permit_id,
        "payload": {"id": 2},
    }
    pending = {**unsigned, "record_hash": sha256_bytes(canonical_json_bytes(unsigned))}
    path.with_suffix(".jsonl.pending.json").write_bytes(canonical_json_bytes(pending))
    assert [row["payload"]["id"] for row in ledger.read_verified()] == [1, 2]
    assert not path.with_suffix(".jsonl.pending.json").exists()


def test_outcome_ledger_requires_prior_earlier_prediction(tmp_path: Path) -> None:
    prediction_ledger = PredictionLedger(
        tmp_path / "ledger" / "predictions.jsonl",
        tmp_path / "anchors",
        clock=_clock(datetime(2026, 7, 15, 20, 2, tzinfo=timezone.utc)),
    )
    prediction = UnderlyingPrediction.create(
        asset_id="asset-1",
        eligibility_census_id="c" * 64,
        symbol="ABC",
        security_type=SecurityType.STOCK,
        decision_session=date(2026, 7, 15),
        decision_at=datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 7, 15, 20, 1, tzinfo=timezone.utc),
        time_authority="SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
        synthetic_clock_permit_id=_clock(
            datetime(2026, 7, 15, 20, 1, tzinfo=timezone.utc)
        ).synthetic_permit_id,
        bundle_id="1" * 64,
        feature_release_id="2" * 64,
        feature_row_hash="1" * 64,
        identity_release_id="3" * 64,
        security_type_evidence_id="4" * 64,
        calendar_release_id="5" * 64,
        action_release_id="6" * 64,
        source_epoch="epoch",
        point_in_time_state="PIT_CONFIRMED",
        prediction_deadline_at=datetime(2026, 7, 15, 20, 5, tzinfo=timezone.utc),
        information_barrier_at=datetime(2026, 7, 22, 22, 0, tzinfo=timezone.utc),
        expected_five_session_return=0.01,
        p_up=0.6,
        p_down=0.3,
        p_neutral=0.1,
        uncertainty=0.02,
        rank=1,
        abstain=False,
        abstention_reason=None,
    )
    prediction_receipt = prediction_ledger.append_synthetic(
        prediction,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="source-selection-prediction",
            scope="SYNTHETIC_SINGLE_PREDICTION_LEDGER_APPEND",
        ),
    )
    prediction_anchor = Path(prediction_receipt["anchor_path"])
    assert prediction_receipt["envelope"]["time_authority"] == (
        "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE"
    )
    anchor_receipt = json.loads((prediction_anchor / "receipt.json").read_text(encoding="utf-8"))
    assert anchor_receipt["time_authority"] == "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE"
    assert len(anchor_receipt["synthetic_clock_permit_id"]) == 64
    outcome = OutcomeRow.create(
        prediction_id=prediction.prediction_id,
        eligibility_census_id=prediction.eligibility_census_id,
        revision_number=1,
        prior_revision_id=None,
        asset_id="asset-1",
        decision_session=date(2026, 7, 15),
        entry_session=date(2026, 7, 16),
        exit_session=date(2026, 7, 22),
        status=OutcomeStatus.MATURED,
        split_normalized_price_return=0.02,
        reason=None,
        calendar_release_id="5" * 64,
        bar_release_id="7" * 64,
        action_release_id="6" * 64,
        source_epoch="epoch",
        action_view_as_of=datetime(2026, 7, 22, 22, 0, tzinfo=timezone.utc),
    )
    ledger = OutcomeLedger(
        tmp_path / "outcomes.jsonl",
        prediction_ledger,
        clock=_clock(datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)),
    )
    with pytest.raises(ContractError, match="after"):
        ledger.append_synthetic(
            outcome,
            prediction_anchor=prediction_anchor,
            synthetic_permit=SyntheticOnlyPermit.create(
                fixture_id="source-selection-outcome-early",
                scope="SYNTHETIC_OUTCOME_LEDGER_APPEND",
            ),
        )
    later_ledger = OutcomeLedger(
        tmp_path / "outcomes.jsonl",
        prediction_ledger,
        clock=_clock(datetime(2026, 7, 22, 22, 1, tzinfo=timezone.utc)),
    )
    outcome_envelope = later_ledger.append_synthetic(
        outcome,
        prediction_anchor=prediction_anchor,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="source-selection-outcome-late",
            scope="SYNTHETIC_OUTCOME_LEDGER_APPEND",
        ),
    )
    assert outcome_envelope["time_authority"] == "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE"
    assert len(later_ledger.verify()) == 1
