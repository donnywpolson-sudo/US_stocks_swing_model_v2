from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.errors import EvaluationAuthorizationError
from us_stocks_swing_model_v2.governance import (
    LocalIntegrityRecord,
    create_local_integrity_record,
)


def test_local_integrity_record_is_content_addressed_and_exact() -> None:
    created_at = datetime(2026, 7, 15, 20, tzinfo=timezone.utc)
    permit = SyntheticOnlyPermit.create(
        fixture_id="local-integrity-matching-clock-authority",
        scope="TRUSTED_CLOCK_FIXED_TIME",
    )
    creation_clock = TrustedClock.synthetic_fixed(created_at, permit=permit)
    observation_clock = TrustedClock.synthetic_fixed(
        created_at + timedelta(seconds=1),
        permit=permit,
    )
    record = create_local_integrity_record(
        scope="AUTHORIZE_TEST_ACTION",
        subject_id="a" * 64,
        bindings={"input_hash": "b" * 64},
        clock=creation_clock,
    )
    assert record.schema_version == 2
    assert record.record_type == "OWNER_OPERATED_LOCAL_INTEGRITY"
    assert LocalIntegrityRecord.from_dict(record.as_dict()) == record
    record.validate(
        expected_scope="AUTHORIZE_TEST_ACTION",
        expected_subject_id="a" * 64,
        required_bindings={"input_hash": "b" * 64},
        clock=observation_clock,
    )
    with pytest.raises(EvaluationAuthorizationError, match="clock authority differs"):
        record.validate(
            expected_scope="AUTHORIZE_TEST_ACTION",
            expected_subject_id="a" * 64,
            required_bindings={"input_hash": "b" * 64},
            clock=TrustedClock.production(),
        )
    other_permit_clock = TrustedClock.synthetic_fixed(
        created_at + timedelta(seconds=1),
        permit=SyntheticOnlyPermit.create(
            fixture_id="local-integrity-different-clock-authority",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )
    with pytest.raises(EvaluationAuthorizationError, match="clock authority differs"):
        record.validate(
            expected_scope="AUTHORIZE_TEST_ACTION",
            expected_subject_id="a" * 64,
            required_bindings={"input_hash": "b" * 64},
            clock=other_permit_clock,
        )

    production_clock = TrustedClock.production()
    production_record = create_local_integrity_record(
        scope="AUTHORIZE_TEST_ACTION",
        subject_id="a" * 64,
        bindings={"input_hash": "b" * 64},
        clock=production_clock,
    )
    production_record.validate(
        expected_scope="AUTHORIZE_TEST_ACTION",
        expected_subject_id="a" * 64,
        required_bindings={"input_hash": "b" * 64},
        clock=production_clock,
    )

    tampered = record.as_dict()
    tampered["bindings"] = {"input_hash": "c" * 64}
    with pytest.raises(EvaluationAuthorizationError, match="record ID"):
        LocalIntegrityRecord.from_dict(tampered)


def test_local_integrity_record_rejects_legacy_signed_schema() -> None:
    legacy = {
        "schema_version": 1,
        "scope": "AUTHORIZE_TEST_ACTION",
        "subject_id": "a" * 64,
        "bindings": {"input_hash": "b" * 64},
        "issued_at": "2026-07-15T20:00:00Z",
        "expires_at": "2026-07-15T20:10:00Z",
        "key_id": "legacy-key",
        "authority_registry_id": "c" * 64,
        "authorization_class": "EXTERNAL_USER_AUTHORITY",
        "signature": "d" * 512,
        "receipt_id": "e" * 64,
    }
    with pytest.raises(EvaluationAuthorizationError, match="fields differ"):
        LocalIntegrityRecord.from_dict(legacy)
