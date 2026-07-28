from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError, NetworkGuardError
from us_stocks_swing_model_v2.providers.identity_publisher import (
    PRODUCTION_STATUS,
    SYNTHETIC_STATUS,
    build_identity_publication_fixture_plan,
    publish_identity_release_fixture,
    verify_identity_release,
)
from us_stocks_swing_model_v2.providers.identity_readiness import (
    ALPACA_ASSETS_MAX_BYTES,
    ALPACA_ASSETS_SOURCE,
    ALPACA_ASSETS_URL,
    IdentityInputAssessment,
    TrustedNasdaqBaseline,
    _assess_loaded_inputs,
    build_alpaca_assets_request_plan,
    guarded_capture_alpaca_assets,
    load_identity_readiness_policy,
)
from us_stocks_swing_model_v2.providers.nasdaq import (
    NASDAQ_TRADED_URL,
    NasdaqCompletenessPolicy,
)
from us_stocks_swing_model_v2.providers.snapshots import AsReceivedSnapshotStore


REPO = Path(__file__).parents[1]
BASELINE_RETRIEVED = datetime(
    2026, 7, 28, 11, 16, 14, 774539, tzinfo=timezone.utc
)
BASELINE_FILE_TIME = datetime(2026, 7, 28, 11, 1, tzinfo=timezone.utc)


def _permit(fixture: str, scope: str = "SYNTHETIC_AS_RECEIVED_SNAPSHOT"):
    return SyntheticOnlyPermit.create(fixture_id=fixture, scope=scope)


def _baseline() -> TrustedNasdaqBaseline:
    return TrustedNasdaqBaseline(
        release_id="1" * 64,
        receipt_id="2" * 64,
        snapshot_id="3" * 64,
        record_count=1,
        retrieved_at=BASELINE_RETRIEVED,
        file_created_at=BASELINE_FILE_TIME,
    )


def _nasdaq_raw(file_time: str = "0729202610:30") -> bytes:
    return (
        "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|"
        "Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n"
        "Y|ABC|ABC COMMON STOCK|N|Q|N|100|N|N|ABC|ABC|N\n"
        f"File Creation Time: {file_time}|||||||||||\n"
    ).encode()


def _assessment(tmp_path: Path) -> IdentityInputAssessment:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    alpaca = store.land(
        source=ALPACA_ASSETS_SOURCE,
        url=ALPACA_ASSETS_URL,
        http_status=200,
        raw=json.dumps(
            [
                {
                    "id": "asset-abc",
                    "symbol": "ABC",
                    "class": "us_equity",
                    "exchange": "NASDAQ",
                    "status": "active",
                    "tradable": True,
                }
            ]
        ).encode(),
        headers={"etag": "assets"},
        retrieved_at=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        synthetic_permit=_permit("assets"),
    )
    nasdaq = store.land(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=_nasdaq_raw(),
        headers={"etag": "nasdaq"},
        retrieved_at=datetime(2026, 7, 29, 15, 1, tzinfo=timezone.utc),
        synthetic_permit=_permit("nasdaq"),
    )
    completeness = NasdaqCompletenessPolicy.synthetic_fixture(
        permit=_permit("nasdaq-policy", "NASDAQ_COMPLETENESS_FIXTURE")
    )
    return _assess_loaded_inputs(
        alpaca_snapshot=alpaca,
        nasdaq_snapshot=nasdaq,
        baseline=_baseline(),
        nasdaq_policy=completeness,
        require_production=False,
    )


def _clock() -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        datetime(2026, 7, 29, 16, 0, tzinfo=timezone.utc),
        permit=_permit("identity-publication-clock", "TRUSTED_CLOCK_FIXED_TIME"),
    )


def test_checked_in_policy_binds_exact_authorization_and_fail_closed_execution() -> None:
    policy = load_identity_readiness_policy(REPO)
    assert (
        policy["authorization_plan_id"]
        == "c34aebff74beee7d256603880c06ae567c8faf21b86f3aadd5f519e197a5c545"
    )
    assert policy["baseline_contract"]["record_count"] == 13064
    assert policy["execution_contract"]["activation"] is False
    assert policy["execution_contract"]["source_config_mutations"] == 0
    assert policy["identity_release_contract"]["role"] == "prospective_as_received"


def test_alpaca_asset_request_is_one_page_bounded_and_plan_only() -> None:
    plan = build_alpaca_assets_request_plan(REPO)
    assert plan.source == ALPACA_ASSETS_SOURCE
    assert plan.initial_url == ALPACA_ASSETS_URL
    assert plan.max_pages == 1
    assert plan.max_response_bytes == ALPACA_ASSETS_MAX_BYTES
    assert plan.timeout_seconds == 30
    assert plan.pagination_parameter == "none"


def test_alpaca_asset_capture_requires_both_network_gate_and_exact_plan(
    tmp_path: Path,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    with pytest.raises(NetworkGuardError, match="network disabled"):
        guarded_capture_alpaca_assets(
            approved_plan_id="f" * 64,
            snapshot_store=store,
            api_key_id="not-used",
            api_secret_key="not-used",
            clock=TrustedClock.production(),
            repo_root=REPO,
            network_enabled=False,
        )


def test_offline_join_requires_both_inputs_strictly_newer_than_baseline(
    tmp_path: Path,
) -> None:
    assessment = _assessment(tmp_path)
    assert assessment.identity_snapshot.trust_eligible is False
    assert assessment.summary()["source_activation"] is False
    assert assessment.nasdaq_record_count == 1
    stale = replace(
        assessment.baseline,
        retrieved_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    alpaca = store.load(
        tmp_path / "snapshots" / ALPACA_ASSETS_SOURCE / assessment.alpaca_snapshot_id
    )
    nasdaq = store.load(
        tmp_path / "snapshots" / "nasdaqtraded" / assessment.nasdaq_snapshot_id
    )
    with pytest.raises(ContractError, match="not newer"):
        _assess_loaded_inputs(
            alpaca_snapshot=alpaca,
            nasdaq_snapshot=nasdaq,
            baseline=stale,
            nasdaq_policy=NasdaqCompletenessPolicy.synthetic_fixture(
                permit=_permit("stale-policy", "NASDAQ_COMPLETENESS_FIXTURE")
            ),
            require_production=False,
        )


def test_synthetic_identity_publication_is_atomic_idempotent_and_non_active(
    tmp_path: Path,
) -> None:
    assessment = _assessment(tmp_path)
    permit = _permit("identity-publication", "IDENTITY_RELEASE_PUBLICATION_FIXTURE")
    plan = build_identity_publication_fixture_plan(
        assessment=assessment,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        permit=permit,
    )
    first = publish_identity_release_fixture(
        plan=plan,
        snapshot=assessment.identity_snapshot,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    second = publish_identity_release_fixture(
        plan=plan,
        snapshot=assessment.identity_snapshot,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    assert first.release_id == second.release_id
    assert first.release_directory == second.release_directory
    receipt = verify_identity_release(
        first.release_directory,
        accepted_root=tmp_path / "accepted",
        expected_plan_id=plan["publication_plan_id"],
        synthetic=True,
    )
    assert receipt["status"] == SYNTHETIC_STATUS
    assert receipt["status"] != PRODUCTION_STATUS
    assert receipt["authorities"]["identity_release_publication"] is False
    assert receipt["authorities"]["source_activation"] is False
    assert receipt["authorities"]["network_calls"] is False


def test_identity_release_verifier_rejects_payload_mutation(tmp_path: Path) -> None:
    assessment = _assessment(tmp_path)
    permit = _permit("identity-mutation", "IDENTITY_RELEASE_PUBLICATION_FIXTURE")
    plan = build_identity_publication_fixture_plan(
        assessment=assessment,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        permit=permit,
    )
    result = publish_identity_release_fixture(
        plan=plan,
        snapshot=assessment.identity_snapshot,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    receipt_path = result.release_directory / "identity_publication_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["authorities"]["source_activation"] = True
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(IntegrityError, match="payload hash mismatch"):
        verify_identity_release(
            result.release_directory,
            accepted_root=tmp_path / "accepted",
            synthetic=True,
        )


def test_synthetic_publication_rejects_wrong_permit_and_production_clock(
    tmp_path: Path,
) -> None:
    assessment = _assessment(tmp_path)
    permit = _permit("identity-bound", "IDENTITY_RELEASE_PUBLICATION_FIXTURE")
    plan = build_identity_publication_fixture_plan(
        assessment=assessment,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        permit=permit,
    )
    wrong = _permit("identity-wrong", "IDENTITY_RELEASE_PUBLICATION_FIXTURE")
    with pytest.raises(ContractError, match="differs from its permit"):
        publish_identity_release_fixture(
            plan=plan,
            snapshot=assessment.identity_snapshot,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            clock=_clock(),
            permit=wrong,
        )
    with pytest.raises(PermissionError, match="synthetic clock"):
        publish_identity_release_fixture(
            plan=plan,
            snapshot=assessment.identity_snapshot,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            clock=TrustedClock.production(),
            permit=permit,
        )
