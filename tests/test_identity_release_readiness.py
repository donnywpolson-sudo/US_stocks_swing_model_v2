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
from us_stocks_swing_model_v2.identity import (
    parse_alpaca_assets,
    project_active_us_equity_assets,
)
from us_stocks_swing_model_v2.providers import (
    identity_publisher as identity_publisher_module,
)
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
    load_alpaca_asset_projection_policy,
    load_identity_readiness_policy,
    select_publication_eligibility_remediation,
)
from us_stocks_swing_model_v2.providers.nasdaq import (
    NASDAQ_TRADED_URL,
    NasdaqCompletenessPolicy,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    AsReceivedSnapshotStore,
    NetworkAcquisitionRegistry,
)


REPO = Path(__file__).parents[1]
BASELINE_RETRIEVED = datetime(
    2026, 7, 28, 11, 16, 14, 774539, tzinfo=timezone.utc
)
BASELINE_FILE_TIME = datetime(2026, 7, 28, 11, 1, tzinfo=timezone.utc)


def _permit(fixture: str, scope: str = "SYNTHETIC_AS_RECEIVED_SNAPSHOT"):
    return SyntheticOnlyPermit.create(fixture_id=fixture, scope=scope)


def _projection_policy():
    return load_alpaca_asset_projection_policy(REPO)


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
        alpaca_projection_policy=_projection_policy(),
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
    assert (
        policy["authorization_plan"]["base_commit"]
        == "ac5c9142172736e820427024be6ddb902cd9c177"
    )
    assert policy["network_registry_id"] == (
        "2f0272f1bb0a3e633bdb30c139ea1eb357bb4fee8e29d5a281cb62c47808e68f"
    )
    registry = policy["publication_eligibility_remediation_registry"]
    historical_id = "c07b748c133722aeb10dc65972b7bc433db6946648f9ae4a783bf2205470a782"
    fresh_id = "fdeaecd1990e3404239e35f3302598fa563f9d57c0e1ee362f5ac6c486c6bd80"
    remediation = registry["records"][historical_id]
    assert (
        remediation["base_commit"]
        == "d83cc2be4d2b5acf46677c101721f0769f1221ba"
    )
    assert remediation["base_tree"] == "9b80c0c5e2484296ef740a345974147387aca122"
    assert (
        remediation["input_assessment_id"]
        == "0ead03c400ad2f3ede1e6545699ed5990c167cc27dcce8637f0402de53f360d8"
    )
    assert (
        remediation["alpaca_snapshot_id"]
        == "2fa24c698c6c000a4f9ab52344c64499253e125664cf7a61e188dc3e45e89efe"
    )
    assert (
        remediation["nasdaq_snapshot_id"]
        == "392382cde8dce26908549315e5c03f831ea7e2e6c17ca00e07a5ab360d8ecdd8"
    )
    assert remediation["required_successor_commit_count"] == 1
    assert remediation["require_clean_tree"] is True
    assert (
        remediation["preserved_authorization_plan_id"]
        == policy["authorization_plan_id"]
    )
    assert (
        policy["publication_eligibility_remediation_registry_id"]
        == "9ce5d00067a0a5766a3643431c5cf9ac77c15e7c922441518006632932085f81"
    )
    assert set(registry["records"]) == {historical_id, fresh_id}
    fresh = registry["records"][fresh_id]
    assert fresh["input_assessment_id"] == "5fc2761e21758dc0ada38ed235f034f865a676dbc9f100a12e17db7763e7f017"
    assert fresh["alpaca_snapshot_id"] == "505f75a085a1e727c00d696e2872c169ceded112dd7b5565c31ecd113417d881"
    assert fresh["nasdaq_snapshot_id"] == "2b86e51b3f7f0c10adac1af29f18f42299ab5140cc0c5f37725c7166719cec80"
    assert fresh["base_commit"] == "7635eb5757049eb24df278474c09d12b3d246135"
    assert fresh["base_tree"] == "684e111bcbfd7514e73df5b3fd4d3fdc83fdbc9d"
    assert policy["baseline_contract"]["record_count"] == 13064
    assert policy["execution_contract"]["activation"] is False
    assert policy["execution_contract"]["source_config_mutations"] == 0
    assert policy["identity_release_contract"]["role"] == "prospective_as_received"
    assert (
        policy["identity_release_contract"]["source_epoch"]
        == "nasdaq_alpaca_active_us_equity_v1"
    )
    assert (
        policy["alpaca_asset_projection_policy_id"]
        == "e6ccdc128a73bc44a8ebdc98a0dcb53d4a5dd4e5bbc236c881fcae89c6ceff68"
    )


@pytest.mark.parametrize(
    ("status", "distance", "message"),
    [
        (" M CODEX_HANDOFF.md", 1, "clean committed tree"),
        ("", 0, "exactly one reviewed successor"),
        ("", 2, "exactly one reviewed successor"),
        ("", 1, None),
    ],
)
def test_publication_repository_gate_requires_clean_single_successor(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    distance: int,
    message: str | None,
) -> None:
    policy = load_identity_readiness_policy(REPO)
    remediation = select_publication_eligibility_remediation(
        policy,
        "c07b748c133722aeb10dc65972b7bc433db6946648f9ae4a783bf2205470a782",
    )
    base = remediation["base_commit"]

    def fake_run_git(root: Path, *arguments: str) -> str:
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(REPO.resolve())
        if arguments == ("rev-parse", f"{base}^{{tree}}"):
            return remediation["base_tree"]
        if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
            return status
        if arguments == ("rev-list", "--count", f"{base}..HEAD"):
            return str(distance)
        if arguments == ("rev-parse", "HEAD"):
            return "c" * 40
        if arguments == ("rev-parse", "HEAD^{tree}"):
            return "d" * 40
        raise AssertionError(f"unexpected Git arguments: {arguments!r}")

    monkeypatch.setattr(identity_publisher_module, "_run_git", fake_run_git)
    monkeypatch.setattr(
        identity_publisher_module.subprocess,
        "run",
        lambda *args, **kwargs: object(),
    )
    if message is None:
        assert identity_publisher_module._repository_binding(
            REPO.resolve(),
            remediation=remediation,
        ) == {"head": "c" * 40, "tree": "d" * 40}
    else:
        with pytest.raises(IntegrityError, match=message):
            identity_publisher_module._repository_binding(
                REPO.resolve(),
                remediation=remediation,
            )


@pytest.mark.parametrize(
    "field",
    [
        "input_assessment_id",
        "alpaca_snapshot_id",
        "nasdaq_snapshot_id",
    ],
)
def test_publication_eligibility_remediation_requires_exact_inputs(
    tmp_path: Path,
    field: str,
) -> None:
    assessment = _assessment(tmp_path)
    remediation = {
        "input_assessment_id": assessment.assessment_id,
        "alpaca_snapshot_id": assessment.alpaca_snapshot_id,
        "nasdaq_snapshot_id": assessment.nasdaq_snapshot_id,
    }
    identity_publisher_module._require_eligibility_remediation_inputs(
        remediation=remediation,
        assessment=assessment,
    )
    remediation[field] = "f" * 64
    with pytest.raises(IntegrityError, match="eligibility remediation"):
        identity_publisher_module._require_eligibility_remediation_inputs(
            remediation=remediation,
            assessment=assessment,
        )


def test_publication_remediation_registry_requires_an_exact_known_id() -> None:
    policy = load_identity_readiness_policy(REPO)
    fresh_id = "fdeaecd1990e3404239e35f3302598fa563f9d57c0e1ee362f5ac6c486c6bd80"
    selected = select_publication_eligibility_remediation(policy, fresh_id)
    assert selected["input_assessment_id"] == "5fc2761e21758dc0ada38ed235f034f865a676dbc9f100a12e17db7763e7f017"
    with pytest.raises(ContractError, match="not registered"):
        select_publication_eligibility_remediation(policy, "f" * 64)
    with pytest.raises(ContractError, match="SHA-256"):
        select_publication_eligibility_remediation(policy, "not-a-record-id")


def test_projection_filters_mixed_assets_without_changing_legacy_parser(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "id": "asset-active",
            "symbol": "AAA",
            "class": "us_equity",
            "exchange": "NASDAQ",
            "status": "active",
            "tradable": True,
        },
        {
            "id": "asset-inactive",
            "symbol": "DUP",
            "class": "us_equity",
            "exchange": "NASDAQ",
            "status": "inactive",
            "tradable": False,
        },
        {
            "id": "crypto-active",
            "symbol": "DUP",
            "class": "crypto",
            "exchange": "CRYPTO",
            "status": "active",
            "tradable": True,
        },
    ]
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    snapshot = store.land(
        source=ALPACA_ASSETS_SOURCE,
        url=ALPACA_ASSETS_URL,
        http_status=200,
        raw=json.dumps(rows).encode(),
        headers={"etag": "mixed-assets"},
        retrieved_at=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        synthetic_permit=_permit("mixed-assets"),
    )
    with pytest.raises(
        ContractError,
        match="well-formed US equity|unique per snapshot",
    ):
        parse_alpaca_assets(snapshot)
    policy = _projection_policy()
    projected = project_active_us_equity_assets(
        snapshot,
        projection_contract=policy["projection_contract"],
        projection_contract_id=policy["projection_contract_id"],
    )
    assert [row.asset_id for row in projected.records] == ["asset-active"]
    assert dict(projected.excluded_counts) == {
        "crypto_active": 1,
        "us_equity_inactive": 1,
    }
    assert projected.raw_record_count == 3


def test_projection_never_silently_deduplicates_selected_symbols(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "id": f"asset-{index}",
            "symbol": "DUP",
            "class": "us_equity",
            "exchange": "NASDAQ",
            "status": "active",
            "tradable": True,
        }
        for index in range(2)
    ]
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    snapshot = store.land(
        source=ALPACA_ASSETS_SOURCE,
        url=ALPACA_ASSETS_URL,
        http_status=200,
        raw=json.dumps(rows).encode(),
        headers={"etag": "duplicate-assets"},
        retrieved_at=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        synthetic_permit=_permit("duplicate-assets"),
    )
    policy = _projection_policy()
    with pytest.raises(ContractError, match="deduplication is prohibited"):
        project_active_us_equity_assets(
            snapshot,
            projection_contract=policy["projection_contract"],
            projection_contract_id=policy["projection_contract_id"],
        )


def test_alpaca_asset_request_is_one_page_bounded_and_plan_only() -> None:
    policy = load_identity_readiness_policy(REPO)
    current_registry = NetworkAcquisitionRegistry.load(
        REPO / "config/network_acquisition_registry.json",
        allowed_root=REPO / "config",
    )
    plan = build_alpaca_assets_request_plan(REPO)
    assert plan.source == ALPACA_ASSETS_SOURCE
    assert plan.initial_url == ALPACA_ASSETS_URL
    assert plan.max_pages == 1
    assert plan.max_response_bytes == ALPACA_ASSETS_MAX_BYTES
    assert plan.timeout_seconds == 30
    assert plan.pagination_parameter == "none"
    assert plan.network_registry_id == current_registry.registry_id
    assert plan.network_registry_id != policy["network_registry_id"]


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
            alpaca_projection_policy=_projection_policy(),
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
    assert receipt["publication_eligibility_remediation_id"] == "2" * 64
    assert (
        receipt["alpaca_projection_contract_id"]
        == assessment.alpaca_projection_contract_id
    )
    assert (
        receipt["alpaca_projection_assessment_id"]
        == assessment.alpaca_projection_assessment_id
    )
    assert receipt["alpaca_selected_record_count"] == assessment.alpaca_record_count
    assert receipt["alpaca_raw_record_count"] == assessment.alpaca_raw_record_count


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
