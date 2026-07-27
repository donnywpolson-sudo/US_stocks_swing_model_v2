from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
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


def _assert_rejected_journal_quarantined(path: Path, raw: bytes) -> None:
    journal_path = path.with_suffix(path.suffix + ".pending.json")
    rejected = path.with_name(
        f".{path.name}.rejected-journal-{sha256_bytes(raw)}.json"
    )
    assert not journal_path.exists()
    assert rejected.read_bytes() == raw


def _validate_fixture_payload(payload: object) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"id"}
        or type(payload["id"]) is not str
        or not payload["id"]
    ):
        raise ContractError("fixture payload requires one nonempty string id")


def _recovery_envelope(
    *,
    sequence: int,
    previous_hash: str,
    payload: dict[str, object],
    recorded_at: str,
    clock: TrustedClock,
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "sequence": sequence,
        "previous_hash": previous_hash,
        "record_type": "fixture_v1",
        "recorded_at": recorded_at,
        "time_authority": clock.mode,
        "synthetic_clock_permit_id": clock.synthetic_permit_id,
        "payload": payload,
    }
    return {
        **unsigned,
        "record_hash": sha256_bytes(canonical_json_bytes(unsigned)),
    }


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
    accepted_root = tmp_path / "pending-review-accepted"
    release = AtomicReleasePublisher(accepted_root).publish(stage, manifest)
    selected = select_explicit_release(
        release,
        expected_dataset="qualification_bars",
        expected_release_id=manifest.release_id,
        expected_project="US_stocks_swing_model_v2",
        accepted_release_root=accepted_root,
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
            accepted_release_root=accepted_root,
            allowed_epoch_roles={"alpaca_qualification_v1": {"qualification_evidence_only"}},
            allowed_quality_states={"QUALIFICATION_EVIDENCE"},
        )
    with pytest.raises(ContractError, match="exact accepted"):
        select_explicit_release(
            release,
            expected_dataset="qualification_bars",
            expected_release_id="0" * 64,
            expected_project="US_stocks_swing_model_v2",
            accepted_release_root=accepted_root,
            allowed_epoch_roles={"alpaca_qualification_v1": {"qualification_evidence_only"}},
            allowed_quality_states={"FAIL"},
        )
    with pytest.raises(ContractError, match="role"):
        select_explicit_release(
            release,
            expected_dataset="qualification_bars",
            expected_release_id=manifest.release_id,
            expected_project="US_stocks_swing_model_v2",
            accepted_release_root=accepted_root,
            allowed_epoch_roles={"alpaca_qualification_v1": {"active_historical"}},
            allowed_quality_states={"FAIL"},
        )
    pending = accepted_root / ".staging" / "fake"
    pending.mkdir(parents=True)
    with pytest.raises(ContractError, match="pending/staging"):
        select_explicit_release(
            pending,
            expected_dataset="qualification_bars",
            expected_release_id=manifest.release_id,
            expected_project="US_stocks_swing_model_v2",
            accepted_release_root=accepted_root,
            allowed_epoch_roles={"alpaca_qualification_v1": {"qualification_evidence_only"}},
            allowed_quality_states={"FAIL"},
        )
    class EpochAlias:
        def __str__(self) -> str:
            return "alpaca_qualification_v1"

    with pytest.raises(ContractError, match="keys must be exact"):
        select_explicit_release(
            release,
            expected_dataset="qualification_bars",
            expected_release_id=manifest.release_id,
            expected_project="US_stocks_swing_model_v2",
            accepted_release_root=accepted_root,
            allowed_epoch_roles={
                EpochAlias(): {"qualification_evidence_only"}
            },
            allowed_quality_states={"FAIL"},
        )


def test_hash_ledger_recovers_precommit_journal_atomically(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
        unique_key="id",
        payload_validator=_validate_fixture_payload,
    )
    first = ledger.append({"id": "first"})
    unsigned = {
        "sequence": 1,
        "previous_hash": first["record_hash"],
        "record_type": "fixture_v1",
        "recorded_at": "2026-07-15T00:01:00Z",
        "time_authority": "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
        "synthetic_clock_permit_id": first["synthetic_clock_permit_id"],
        "payload": {"id": "second"},
    }
    pending = {**unsigned, "record_hash": sha256_bytes(canonical_json_bytes(unsigned))}
    expected_bytes = path.read_bytes() + canonical_json_bytes(pending)
    journal_path = path.with_suffix(".jsonl.pending.json")
    journal_path.write_bytes(canonical_json_bytes(pending))
    recovered = ledger.read_verified()
    assert [row["payload"]["id"] for row in recovered] == ["first", "second"]
    assert path.read_bytes() == expected_bytes
    assert not journal_path.exists()
    assert HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
        unique_key="id",
        payload_validator=_validate_fixture_payload,
    ).read_verified() == recovered


def test_hash_ledger_recovery_rejects_invalid_payload_before_empty_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-payload.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
        unique_key="id",
        payload_validator=_validate_fixture_payload,
    )
    pending = _recovery_envelope(
        sequence=0,
        previous_hash="0" * 64,
        payload={"id": 7},
        recorded_at="2026-07-15T00:00:00Z",
        clock=clock,
    )
    pending_bytes = canonical_json_bytes(pending)
    path.with_suffix(".jsonl.pending.json").write_bytes(pending_bytes)

    with pytest.raises(IntegrityError, match="record-type validator"):
        ledger.read_verified()

    assert not path.exists()
    _assert_rejected_journal_quarantined(path, pending_bytes)


@pytest.mark.parametrize("existing_record", [False, True])
def test_hash_ledger_recovery_rejects_duplicate_logical_keys(
    tmp_path: Path,
    existing_record: bool,
) -> None:
    path = tmp_path / f"duplicate-{existing_record}.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
        unique_key="id",
        payload_validator=_validate_fixture_payload,
    )
    if existing_record:
        first = ledger.append({"id": "duplicate"})
        pending = _recovery_envelope(
            sequence=1,
            previous_hash=first["record_hash"],
            payload={"id": "duplicate"},
            recorded_at="2026-07-15T00:01:00Z",
            clock=clock,
        )
        journal: object = pending
    else:
        first = _recovery_envelope(
            sequence=0,
            previous_hash="0" * 64,
            payload={"id": "duplicate"},
            recorded_at="2026-07-15T00:00:00Z",
            clock=clock,
        )
        second = _recovery_envelope(
            sequence=1,
            previous_hash=str(first["record_hash"]),
            payload={"id": "duplicate"},
            recorded_at="2026-07-15T00:01:00Z",
            clock=clock,
        )
        journal = {
            "batch_schema_version": 1,
            "record_type": "fixture_v1",
            "envelopes": [first, second],
        }
    pending_bytes = canonical_json_bytes(journal)
    path.with_suffix(".jsonl.pending.json").write_bytes(pending_bytes)
    committed_bytes = path.read_bytes() if path.exists() else b""

    with pytest.raises(IntegrityError, match="duplicates append-only key"):
        ledger.read_verified()

    assert (path.read_bytes() if path.exists() else b"") == committed_bytes
    _assert_rejected_journal_quarantined(path, pending_bytes)


def test_hash_ledger_revalidates_an_already_committed_recovery_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "already-committed-duplicate.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
        unique_key="id",
        payload_validator=_validate_fixture_payload,
    )
    first = ledger.append({"id": "duplicate"})
    duplicate = _recovery_envelope(
        sequence=1,
        previous_hash=first["record_hash"],
        payload={"id": "duplicate"},
        recorded_at="2026-07-15T00:01:00Z",
        clock=clock,
    )
    duplicate_bytes = canonical_json_bytes(duplicate)
    path.write_bytes(path.read_bytes() + duplicate_bytes)
    path.with_suffix(".jsonl.pending.json").write_bytes(duplicate_bytes)
    committed_bytes = path.read_bytes()

    with pytest.raises(IntegrityError, match="duplicates append-only key"):
        ledger.read_verified()

    assert path.read_bytes() == committed_bytes
    _assert_rejected_journal_quarantined(path, duplicate_bytes)


@pytest.mark.parametrize("append_method", ["append", "append_many"])
def test_hash_ledger_rejects_semantically_invalid_committed_history_before_append(
    tmp_path: Path,
    append_method: str,
) -> None:
    path = tmp_path / f"invalid-committed-{append_method}.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    invalid = _recovery_envelope(
        sequence=0,
        previous_hash="0" * 64,
        payload={"id": 7},
        recorded_at="2026-07-15T00:00:00Z",
        clock=clock,
    )
    committed_bytes = canonical_json_bytes(invalid)
    path.write_bytes(committed_bytes)
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
        unique_key="id",
        payload_validator=_validate_fixture_payload,
    )

    with pytest.raises(ContractError, match="fixture payload requires"):
        if append_method == "append":
            ledger.append({"id": "new"})
        else:
            ledger.append_many(
                ({"id": "new"},),
                expected_record_count=1,
                expected_head_hash=str(invalid["record_hash"]),
            )

    assert path.read_bytes() == committed_bytes
    assert not path.with_suffix(".jsonl.pending.json").exists()


@pytest.mark.parametrize("append_method", ["append", "append_many"])
def test_hash_ledger_rejects_duplicate_committed_history_before_append(
    tmp_path: Path,
    append_method: str,
) -> None:
    path = tmp_path / f"duplicate-committed-{append_method}.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    first = _recovery_envelope(
        sequence=0,
        previous_hash="0" * 64,
        payload={"id": "duplicate"},
        recorded_at="2026-07-15T00:00:00Z",
        clock=clock,
    )
    second = _recovery_envelope(
        sequence=1,
        previous_hash=str(first["record_hash"]),
        payload={"id": "duplicate"},
        recorded_at="2026-07-15T00:01:00Z",
        clock=clock,
    )
    committed_bytes = canonical_json_bytes(first) + canonical_json_bytes(second)
    path.write_bytes(committed_bytes)
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
        unique_key="id",
        payload_validator=_validate_fixture_payload,
    )

    with pytest.raises(
        IntegrityError,
        match="committed ledger duplicates append-only key",
    ):
        if append_method == "append":
            ledger.append({"id": "new"})
        else:
            ledger.append_many(
                ({"id": "new"},),
                expected_record_count=2,
                expected_head_hash=str(second["record_hash"]),
            )

    assert path.read_bytes() == committed_bytes
    assert not path.with_suffix(".jsonl.pending.json").exists()


def test_hash_ledger_rejects_append_invariants_not_bound_at_construction(
    tmp_path: Path,
) -> None:
    ledger = HashChainLedger(
        tmp_path / "unbound.jsonl",
        "fixture_v1",
        clock=_clock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
    )

    with pytest.raises(ContractError, match="bound at construction"):
        ledger.append({"id": "value"}, unique_key="id")
    with pytest.raises(ContractError, match="bound at construction"):
        ledger.append(
            {"id": "value"},
            payload_validator=_validate_fixture_payload,
        )


def test_hash_ledger_recovers_pending_append_from_authorized_prior_permit(
    tmp_path: Path,
) -> None:
    first_clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    rebound_clock = _clock(datetime(2026, 7, 15, 0, 1, tzinfo=timezone.utc))
    unsigned = {
        "sequence": 0,
        "previous_hash": "0" * 64,
        "record_type": "fixture_v1",
        "recorded_at": "2026-07-15T00:00:00Z",
        "time_authority": "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
        "synthetic_clock_permit_id": first_clock.synthetic_permit_id,
        "payload": {"id": 1},
    }
    pending = {
        **unsigned,
        "record_hash": sha256_bytes(canonical_json_bytes(unsigned)),
    }
    path = tmp_path / "authorized" / "ledger.jsonl"
    path.parent.mkdir()
    journal_path = path.with_suffix(".jsonl.pending.json")
    pending_bytes = canonical_json_bytes(pending)
    journal_path.write_bytes(pending_bytes)
    ledger = HashChainLedger(path, "fixture_v1", clock=rebound_clock)
    permit_ids = tuple(
        sorted(
            (
                first_clock.synthetic_permit_id,
                rebound_clock.synthetic_permit_id,
            )
        )
    )
    ledger.authorize_synthetic_history(
        permit_ids,
        permit=SyntheticOnlyPermit.create(
            fixture_id="authorized-pending-ledger-history",
            scope="SYNTHETIC_LEDGER_HISTORY_PERMITS",
        ),
    )
    recovered = ledger.read_verified()
    assert recovered == [pending]
    assert path.read_bytes() == pending_bytes
    assert not journal_path.exists()
    fresh = HashChainLedger(path, "fixture_v1", clock=rebound_clock)
    fresh.authorize_synthetic_history(
        permit_ids,
        permit=SyntheticOnlyPermit.create(
            fixture_id="authorized-pending-ledger-history-fresh-reader",
            scope="SYNTHETIC_LEDGER_HISTORY_PERMITS",
        ),
    )
    assert fresh.read_verified() == recovered

    unauthorized_path = tmp_path / "unauthorized" / "ledger.jsonl"
    unauthorized_path.parent.mkdir()
    foreign_clock = _clock(datetime(2026, 7, 15, 0, 2, tzinfo=timezone.utc))
    foreign_unsigned = {
        **unsigned,
        "synthetic_clock_permit_id": foreign_clock.synthetic_permit_id,
    }
    foreign_pending = {
        **foreign_unsigned,
        "record_hash": sha256_bytes(canonical_json_bytes(foreign_unsigned)),
    }
    foreign_pending_bytes = canonical_json_bytes(foreign_pending)
    unauthorized_path.with_suffix(".jsonl.pending.json").write_bytes(
        foreign_pending_bytes
    )
    unauthorized = HashChainLedger(
        unauthorized_path,
        "fixture_v1",
        clock=rebound_clock,
    )
    unauthorized.authorize_synthetic_history(
        permit_ids,
        permit=SyntheticOnlyPermit.create(
            fixture_id="unauthorized-pending-ledger-history",
            scope="SYNTHETIC_LEDGER_HISTORY_PERMITS",
        ),
    )
    with pytest.raises(IntegrityError, match="not authorized"):
        unauthorized.read_verified()
    assert not unauthorized_path.exists()
    _assert_rejected_journal_quarantined(
        unauthorized_path,
        foreign_pending_bytes,
    )
    assert HashChainLedger(
        unauthorized_path,
        "fixture_v1",
        clock=rebound_clock,
    ).read_verified() == []


def test_hash_ledger_rejects_records_from_another_synthetic_clock_permit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    first_clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    HashChainLedger(path, "fixture_v1", clock=first_clock).append({"id": 1})
    other_clock = _clock(datetime(2026, 7, 15, 0, 1, tzinfo=timezone.utc))
    with pytest.raises(IntegrityError, match="not authorized by this verifier"):
        HashChainLedger(path, "fixture_v1", clock=other_clock).read_verified()
    assert not path.with_suffix(".jsonl.pending.json").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_field",
        "sequence",
        "predecessor",
        "record_type",
        "timestamp",
        "time_authority",
        "permit_binding",
        "wrong_permit",
        "payload_type",
        "record_hash",
        "nonmonotone",
        "altered_replay",
    ],
)
def test_hash_ledger_recovery_rejects_adversarial_envelope_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = tmp_path / "ledger.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
    )
    first = ledger.append({"id": 1})
    unsigned: dict[str, object] = {
        "sequence": 1,
        "previous_hash": first["record_hash"],
        "record_type": "fixture_v1",
        "recorded_at": "2026-07-15T00:01:00Z",
        "time_authority": "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
        "synthetic_clock_permit_id": first["synthetic_clock_permit_id"],
        "payload": {"id": 2},
    }
    if mutation == "extra_field":
        unsigned["unexpected"] = True
    elif mutation == "sequence":
        unsigned["sequence"] = 2
    elif mutation == "predecessor":
        unsigned["previous_hash"] = "0" * 64
    elif mutation == "record_type":
        unsigned["record_type"] = "other_v1"
    elif mutation == "timestamp":
        unsigned["recorded_at"] = "not-a-time"
    elif mutation == "time_authority":
        unsigned["time_authority"] = "PRODUCTION_SYSTEM_UTC"
    elif mutation == "permit_binding":
        unsigned["synthetic_clock_permit_id"] = None
    elif mutation == "wrong_permit":
        unsigned["synthetic_clock_permit_id"] = "f" * 64
    elif mutation == "payload_type":
        unsigned["payload"] = ["not", "an", "object"]
    elif mutation == "nonmonotone":
        unsigned["recorded_at"] = "2026-07-14T23:59:59Z"
    elif mutation == "altered_replay":
        unsigned = {
            key: first[key]
            for key in (
                "sequence",
                "previous_hash",
                "record_type",
                "recorded_at",
                "time_authority",
                "synthetic_clock_permit_id",
                "payload",
            )
        }
        unsigned["payload"] = {"id": 999}
    pending = {
        **unsigned,
        "record_hash": sha256_bytes(canonical_json_bytes(unsigned)),
    }
    if mutation == "record_hash":
        pending["record_hash"] = "0" * 64
    pending_bytes = canonical_json_bytes(pending)
    path.with_suffix(".jsonl.pending.json").write_bytes(pending_bytes)
    committed_bytes = path.read_bytes()

    with pytest.raises(IntegrityError):
        ledger.read_verified()
    assert path.read_bytes() == committed_bytes
    _assert_rejected_journal_quarantined(path, pending_bytes)
    assert [
        row["payload"]["id"]
        for row in HashChainLedger(path, "fixture_v1", clock=clock).read_verified()
    ] == [1]


def test_hash_ledger_recovery_rejects_truncated_journal(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
    )
    pending_bytes = b'{"sequence":'
    path.with_suffix(".jsonl.pending.json").write_bytes(pending_bytes)
    with pytest.raises(IntegrityError, match="journal is invalid"):
        ledger.read_verified()
    _assert_rejected_journal_quarantined(path, pending_bytes)
    assert HashChainLedger(path, "fixture_v1", clock=clock).read_verified() == []


@pytest.mark.parametrize("envelopes", [[None], [{"sequence": 0}]])
def test_hash_ledger_recovery_rejects_malformed_batch_envelopes(
    tmp_path: Path,
    envelopes: list[object],
) -> None:
    path = tmp_path / "ledger.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
    )
    journal = {
        "batch_schema_version": 1,
        "record_type": "fixture_v1",
        "envelopes": envelopes,
    }
    pending_bytes = canonical_json_bytes(journal)
    path.with_suffix(".jsonl.pending.json").write_bytes(pending_bytes)
    with pytest.raises(IntegrityError):
        ledger.read_verified()
    assert not path.exists()
    _assert_rejected_journal_quarantined(path, pending_bytes)
    assert HashChainLedger(path, "fixture_v1", clock=clock).read_verified() == []


@pytest.mark.parametrize("pending", [[], "text", 7, True, None])
def test_hash_ledger_rejects_non_object_recovery_journal(
    tmp_path: Path, pending: object
) -> None:
    path = tmp_path / "ledger.jsonl"
    clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    ledger = HashChainLedger(
        path,
        "fixture_v1",
        clock=clock,
    )
    pending_bytes = json.dumps(pending).encode("utf-8")
    path.with_suffix(".jsonl.pending.json").write_bytes(pending_bytes)
    with pytest.raises(IntegrityError, match="must be a JSON object"):
        ledger.read_verified()
    _assert_rejected_journal_quarantined(path, pending_bytes)
    assert HashChainLedger(path, "fixture_v1", clock=clock).read_verified() == []


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
        tmp_path / "outcome-ledger" / "outcomes.jsonl",
        prediction_ledger,
        anchor_root=tmp_path / "outcome-anchors",
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
        tmp_path / "outcome-ledger" / "outcomes.jsonl",
        prediction_ledger,
        anchor_root=tmp_path / "outcome-anchors",
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
    assert outcome_envelope["envelope"]["time_authority"] == (
        "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE"
    )
    assert len(
        later_ledger.verify(Path(outcome_envelope["anchor_path"]))
    ) == 1
    outcome_path = tmp_path / "outcome-ledger" / "outcomes.jsonl"
    committed = outcome_path.read_bytes()
    outcome_path.write_bytes(b"")
    with pytest.raises(
        IntegrityError,
        match="local tamper-evident head anchor",
    ):
        later_ledger.verify(Path(outcome_envelope["anchor_path"]))
    outcome_path.write_bytes(committed)
