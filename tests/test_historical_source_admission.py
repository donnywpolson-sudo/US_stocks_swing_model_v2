from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.causal_foundation import (
    AvailabilityStamp,
    CausalDailyBar,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.historical_source_admission import (
    MANDATORY_SOURCE_FAMILIES,
    CorpusValidation,
    HistoricalIdentityInterval,
    HistoricalRawDailyBar,
    SourcePackageDescriptor,
    assess_source_package,
    audit_identity_intervals,
    audit_raw_daily_bars,
    build_structural_universe_view,
    load_content_addressed_source_record,
    load_v1_admission_policy,
    load_v1_source_contract,
    require_qualified_source_bundle,
    require_source_admitted_bar,
    visible_identity_as_of,
)
from us_stocks_swing_model_v2.outcome_firewall import (
    FoundationDataGateway,
    OutcomeAccessDenied,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ID = "a0b3e22afc7f563174763c9f03ec719c5052bbb5972aa8b52674f3e03d5ce88a"
POLICY_ID = "76bf376faf2cb5b6b660bc37bb7777d0c8979eca278292212501d573eb9a91d8"
UTC = timezone.utc


def _hash(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def _claims(**changes: bool) -> dict[str, bool]:
    value = {
        "active_and_inactive_coverage": True,
        "corporate_action_effective_coverage": True,
        "corporate_action_publication_times": True,
        "corporate_action_revision_history": True,
        "delisted_coverage": True,
        "full_lineage": True,
        "historical_exchange_listing_validity": True,
        "historical_revisions_retained": True,
        "historical_ticker_validity": True,
        "no_current_state_join": True,
        "no_forward_fill": True,
        "no_interpolation": True,
        "no_synthetic_rows": True,
        "raw_bytes_immutable": True,
        "security_type_explicit": True,
        "session_date_qualified": True,
        "stable_security_identifier": True,
        "terminal_event_representation": True,
        "timestamp_publication_semantics": True,
    }
    value.update(changes)
    return value


def _descriptor(
    family: str,
    *,
    evidence_class: str = "EXTERNAL_AS_RECEIVED",
    adjustment_state: str | None = None,
    license_classification: str = "LOCAL_RESEARCH_PERMITTED",
    claims: dict[str, bool] | None = None,
    source_identifier: str | None = None,
    storage_location: str | None = None,
) -> SourcePackageDescriptor:
    file_hash = _hash(f"source-bytes-{family}")
    payload = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "record_type": "HISTORICAL_SOURCE_PACKAGE_DESCRIPTOR",
        "source_identifier": source_identifier or f"fixture-{family.casefold()}",
        "source_family": family,
        "provider": "CONTRACT_MECHANICS_FIXTURE",
        "dataset_name": f"fixture-{family.casefold()}",
        "dataset_version": "fixture-v1",
        "dataset_schema_version": "fixture-schema-v1",
        "retrieved_at": "2026-08-13T22:00:00Z",
        "coverage_start": "2016-01-04",
        "coverage_end": "2026-08-13",
        "security_scope": "DETERMINISTIC_CONTRACT_MECHANICS_FIXTURE",
        "identifier_fields": ["stable_security_id"],
        "adjustment_state": adjustment_state
        or ("RAW_UNADJUSTED" if family == "RAW_DAILY_OHLCV" else "NOT_APPLICABLE"),
        "timezone": "America/New_York_WITH_UTC_BOUNDARIES",
        "timestamp_semantics": "QUALIFIED_FIXTURE_SEMANTICS",
        "revision_policy": "APPEND_ONLY_VINTAGES",
        "license_classification": license_classification,
        "storage_location": storage_location
        or f"observations/qualified-{family.casefold().replace('_', '-')}",
        "file_manifest": [
            {
                "path": "raw/partition.bin",
                "bytes": 24,
                "sha256": file_hash,
            }
        ],
        "content_hashes": [file_hash],
        "schema_hash": _hash(f"schema-{family}"),
        "ingestion_code_version": "fixture-ingestion-v1",
        "known_limitations": [],
        "evidence_class": evidence_class,
        "semantic_claims": claims or _claims(),
    }
    payload["descriptor_id"] = sha256_bytes(canonical_json_bytes(payload))
    return SourcePackageDescriptor.from_dict(payload)


def _validation(**changes: int | bool) -> CorpusValidation:
    counts = {
        "current_state_join_rows": 0,
        "duplicate_key_rows": 0,
        "forward_filled_rows": 0,
        "future_availability_violations": 0,
        "invalid_bar_rows": 0,
        "invalid_exchange_rows": 0,
        "invalid_historical_ticker_rows": 0,
        "invalid_rows": 0,
        "interpolated_rows": 0,
        "missing_lineage_rows": 0,
        "quarantined_rows": 0,
        "silent_dropped_inactive_rows": 0,
        "synthetic_rows": 0,
        "unexpected_session_rows": 0,
        "unknown_security_type_rows": 0,
        "unresolved_corporate_action_rows": 0,
        "unresolved_identity_rows": 0,
    }
    flag_names = {
        "full_corpus",
        "file_hashes_verified",
        "schema_hash_verified",
        "source_count_reconciled",
    }
    flags = {name: bool(changes.pop(name, True)) for name in flag_names}
    counts.update({key: int(value) for key, value in changes.items()})
    quarantined = counts["quarantined_rows"]
    return CorpusValidation.create(
        total_rows=2,
        validated_rows=2 - quarantined,
        counts=counts,
        **flags,
    )


def _admit(descriptor: SourcePackageDescriptor, validation: CorpusValidation | None = None):
    return assess_source_package(
        descriptor,
        validation or _validation(),
        contract_id=CONTRACT_ID,
        policy_id=POLICY_ID,
    )


def _stamp(name: str, when: datetime) -> AvailabilityStamp:
    return AvailabilityStamp(
        effective_time=when,
        published_time=when,
        received_time=when,
        usable_time=when,
        source_revision="1",
        source_identifier=f"fixture-{name}",
        source_snapshot_id=_hash(f"snapshot-{name}-{when.isoformat()}"),
    )


def _identity(
    stable_id: str,
    ticker: str,
    start: date,
    end: date | None,
    *,
    usable: datetime,
    security_type: str = "COMMON_STOCK",
    listing_state: str = "ACTIVE",
    mic: str = "XNYS",
) -> HistoricalIdentityInterval:
    return HistoricalIdentityInterval.create(
        stable_security_id=stable_id,
        vendor_instrument_id=f"vendor-{stable_id}",
        issuer_id=f"issuer-{stable_id}",
        share_class_id=f"class-{stable_id}",
        ticker=ticker,
        mic=mic,
        security_type=security_type,
        listing_state=listing_state,
        effective_start=start,
        effective_end=end,
        availability=_stamp(f"identity-{stable_id}-{start}", usable),
        revision_number=1,
        predecessor_row_id=None,
        source_row_hash=_hash(f"identity-row-{stable_id}-{ticker}-{start}"),
    )


def _bar(
    stable_id: str,
    ticker: str,
    session: date,
    *,
    usable: datetime,
    close: float = 10.5,
    volume: int = 1000,
    synthetic: bool = False,
) -> HistoricalRawDailyBar:
    return HistoricalRawDailyBar.create(
        stable_security_id=stable_id,
        source_symbol=ticker,
        session=session,
        open=10.0,
        high=max(11.0, close),
        low=9.5,
        close=close,
        volume=volume,
        availability=_stamp(f"bar-{stable_id}-{session}", usable),
        source_partition_hash=_hash(f"partition-{session}"),
        source_row_hash=_hash(f"bar-row-{stable_id}-{session}"),
        synthetic=synthetic,
        interpolated=False,
        forward_filled=False,
        halted=False,
    )


def test_source_scope_contract_policy_quarantine_and_acquisition_records_are_exact() -> None:
    contract = load_v1_source_contract(ROOT / "config/historical_source_contract_v1.json")
    policy = load_v1_admission_policy(
        ROOT / "config/historical_source_admission_policy_v1.json"
    )
    scope = load_content_addressed_source_record(
        ROOT / "config/v1_price_volume_research_scope.json",
        id_field="scope_id",
    )
    quarantine = load_content_addressed_source_record(
        ROOT / "config/legacy_historical_data_quarantine_v1.json",
        id_field="quarantine_id",
    )
    requirements = load_content_addressed_source_record(
        ROOT / "config/historical_source_acquisition_requirements_v1.json",
        id_field="requirements_id",
    )

    assert contract["contract_id"] == CONTRACT_ID
    assert policy["policy_id"] == POLICY_ID
    assert scope["data_foundation_only"] is True
    assert scope["security_type_policy"]["included_v1"] == ["COMMON_STOCK"]
    assert "HISTORICAL_INDEX_MEMBERSHIP" in scope["out_of_scope_v1"]
    assert quarantine["physical_files_modified"] is False
    assert all(
        "NOT_RESEARCH_ELIGIBLE" in item["classifications"]
        for item in quarantine["sources"]
    )
    assert requirements["this_record_authorizes_bulk_download"] is False
    assert requirements["status"] == "BLOCKED_EXTERNAL_SOURCE_PACKAGE_REQUIRED"


def test_admission_is_idempotent_and_all_mandatory_families_are_required() -> None:
    results = tuple(_admit(_descriptor(family)) for family in MANDATORY_SOURCE_FAMILIES)
    repeated = tuple(_admit(_descriptor(family)) for family in MANDATORY_SOURCE_FAMILIES)
    assert results == repeated
    assert all(result.status == "ADMITTED" for result in results)
    bundle = require_qualified_source_bundle(results)
    assert tuple(item.source_family for item in bundle.admissions) == MANDATORY_SOURCE_FAMILIES
    with pytest.raises(ContractError, match="mandatory family"):
        require_qualified_source_bundle(results[:-1])


def test_legacy_synthetic_adjusted_and_incomplete_sources_fail_closed() -> None:
    legacy = _admit(
        _descriptor("RAW_DAILY_OHLCV", evidence_class="LEGACY_DISCOVERY")
    )
    assert legacy.status == "QUARANTINED"
    assert legacy.admitted_rows == 0
    assert "LEGACY_DISCOVERY_NOT_RESEARCH_ELIGIBLE" in legacy.reason_codes

    synthetic = _admit(
        _descriptor("RAW_DAILY_OHLCV", evidence_class="SYNTHETIC_ONLY")
    )
    assert synthetic.status == "SYNTHETIC_TEST_ONLY"
    assert synthetic.research_eligible is False

    adjusted = _admit(
        _descriptor(
            "RAW_DAILY_OHLCV",
            adjustment_state="ADJUSTED_CONVENIENCE_ONLY",
        )
    )
    assert adjusted.status == "BLOCKED"
    assert "ADJUSTED_ONLY_SOURCE_DENIED" in adjusted.reason_codes
    assert "RAW_OHLCV_REQUIRED" in adjusted.reason_codes

    incomplete = _admit(
        _descriptor(
            "HISTORICAL_SECURITY_MASTER",
            claims=_claims(historical_ticker_validity=False),
        ),
        _validation(quarantined_rows=1, unresolved_identity_rows=1),
    )
    assert incomplete.status == "BLOCKED"
    assert "MISSING_SEMANTIC_HISTORICAL_TICKER_VALIDITY" in incomplete.reason_codes
    assert "NONZERO_UNRESOLVED_IDENTITY_ROWS" in incomplete.reason_codes
    assert incomplete.quarantined_rows == 2


def test_firewall_requires_exact_source_admission_and_storage_binding(tmp_path: Path) -> None:
    package = tmp_path / "observations" / "qualified-bars"
    package.mkdir(parents=True)
    target = package / "partition.json"
    target.write_text("{}", encoding="utf-8")
    gateway = FoundationDataGateway(tmp_path.resolve())
    admitted = _admit(
        _descriptor(
            "RAW_DAILY_OHLCV",
            storage_location="observations/qualified-bars",
        )
    )
    assert gateway.resolve_source_qualified_input(
        "observations/qualified-bars/partition.json",
        admission=admitted,
        purpose="SOURCE_QUALIFIED_CANONICAL_INPUT",
        requested_at=datetime(2026, 8, 13, 22, 0, tzinfo=UTC),
    ) == target
    assert gateway.audit_events[-1].decision == "ALLOW_QUALIFIED_SOURCE"

    legacy = _admit(
        _descriptor(
            "RAW_DAILY_OHLCV",
            evidence_class="LEGACY_DISCOVERY",
            storage_location="observations/qualified-bars",
        )
    )
    with pytest.raises(OutcomeAccessDenied, match="not admitted"):
        gateway.resolve_source_qualified_input(
            "observations/qualified-bars/partition.json",
            admission=legacy,
            purpose="SOURCE_QUALIFIED_CANONICAL_INPUT",
            requested_at=datetime(2026, 8, 13, 22, 1, tzinfo=UTC),
        )
    with pytest.raises(OutcomeAccessDenied, match="package location"):
        gateway.resolve_source_qualified_input(
            "observations/other/partition.json",
            admission=admitted,
            purpose="SOURCE_QUALIFIED_CANONICAL_INPUT",
            requested_at=datetime(2026, 8, 13, 22, 2, tzinfo=UTC),
        )


def test_ticker_reuse_and_future_current_snapshot_poisoning_do_not_rewrite_history() -> None:
    early_known = datetime(2019, 12, 31, 22, 0, tzinfo=UTC)
    later_known = datetime(2021, 12, 31, 22, 0, tzinfo=UTC)
    first = _identity(
        "security-a",
        "XYZ",
        date(2020, 1, 2),
        date(2021, 12, 31),
        usable=early_known,
    )
    reused = _identity(
        "security-b",
        "XYZ",
        date(2022, 1, 3),
        None,
        usable=later_known,
    )
    rows = (first, reused)
    audit = audit_identity_intervals(rows)
    assert audit.ticker_reuse_count == 1
    assert audit.ambiguous_overlap_count == 0
    cutoff = datetime(2021, 6, 1, 22, 0, tzinfo=UTC)
    before = visible_identity_as_of(
        rows,
        session=date(2021, 6, 1),
        signal_cutoff=cutoff,
    )
    assert [(row.stable_security_id, row.ticker) for row in before] == [
        ("security-a", "XYZ")
    ]

    poisoned_current_snapshot = _identity(
        "current-only-security",
        "XYZ",
        date(2026, 8, 13),
        None,
        usable=datetime(2026, 8, 13, 22, 0, tzinfo=UTC),
    )
    after = visible_identity_as_of(
        (*rows, poisoned_current_snapshot),
        session=date(2021, 6, 1),
        signal_cutoff=cutoff,
    )
    assert after == before


def test_overlapping_ticker_or_security_intervals_are_ambiguous() -> None:
    known = datetime(2019, 12, 31, 22, 0, tzinfo=UTC)
    first = _identity(
        "security-a",
        "XYZ",
        date(2020, 1, 2),
        date(2022, 1, 31),
        usable=known,
    )
    overlap = _identity(
        "security-b",
        "XYZ",
        date(2021, 1, 4),
        None,
        usable=known,
    )
    assert audit_identity_intervals((first, overlap)).ambiguous_overlap_count == 1
    with pytest.raises(ContractError, match="ticker mapping is ambiguous"):
        visible_identity_as_of(
            (first, overlap),
            session=date(2021, 6, 1),
            signal_cutoff=datetime(2021, 6, 1, 22, 0, tzinfo=UTC),
        )


def test_raw_bar_audit_reconciles_every_row_without_repair_or_silent_drop() -> None:
    session = date(2021, 6, 1)
    close_at = datetime(2021, 6, 1, 20, 0, tzinfo=UTC)
    usable = datetime(2021, 6, 1, 20, 1, tzinfo=UTC)
    identity = _identity(
        "security-a",
        "AAA",
        date(2020, 1, 2),
        None,
        usable=datetime(2020, 1, 2, 22, 0, tzinfo=UTC),
    )
    valid = _bar("security-a", "AAA", session, usable=usable)
    report = audit_raw_daily_bars(
        (valid,),
        identities=(identity,),
        session_closes={session: close_at},
    )
    assert report.total_rows == 1
    assert report.admitted_rows == 1
    assert report.quarantined_rows == 0

    duplicate = replace(valid, source_row_hash=_hash("different-row"))
    duplicate = replace(
        duplicate,
        row_id=sha256_bytes(canonical_json_bytes(duplicate.unsigned_dict())),
    )
    duplicate_report = audit_raw_daily_bars(
        (valid, duplicate),
        identities=(identity,),
        session_closes={session: close_at},
    )
    assert duplicate_report.admitted_rows == 0
    assert duplicate_report.quarantined_rows == 2
    assert dict(duplicate_report.reason_counts)["DUPLICATE_SECURITY_DATE"] == 2

    synthetic = _bar(
        "security-a",
        "AAA",
        date(2021, 6, 2),
        usable=datetime(2021, 6, 2, 20, 1, tzinfo=UTC),
        synthetic=True,
    )
    synthetic_report = audit_raw_daily_bars(
        (synthetic,),
        identities=(identity,),
        session_closes={date(2021, 6, 2): datetime(2021, 6, 2, 20, 0, tzinfo=UTC)},
    )
    assert dict(synthetic_report.reason_counts) == {"SYNTHETIC_BAR": 1}


def test_structural_universe_is_cutoff_safe_and_keeps_threshold_policy_separate() -> None:
    session = date(2021, 6, 1)
    cutoff = datetime(2021, 6, 1, 20, 5, tzinfo=UTC)
    common = _identity(
        "security-a",
        "AAA",
        date(2020, 1, 2),
        None,
        usable=datetime(2020, 1, 2, 22, 0, tzinfo=UTC),
    )
    fund = _identity(
        "security-etf",
        "FUND",
        date(2020, 1, 2),
        None,
        usable=datetime(2020, 1, 2, 22, 0, tzinfo=UTC),
        security_type="ETF",
    )
    common_bar = _bar(
        "security-a",
        "AAA",
        session,
        usable=datetime(2021, 6, 1, 20, 1, tzinfo=UTC),
    )
    fund_bar = _bar(
        "security-etf",
        "FUND",
        session,
        usable=datetime(2021, 6, 1, 20, 1, tzinfo=UTC),
    )
    before = build_structural_universe_view(
        identity_rows=(common, fund),
        bars=(common_bar, fund_bar),
        session=session,
        signal_cutoff=cutoff,
    )
    assert before[0].structural_eligible is True
    assert before[0].reason_codes == ("ELIGIBLE_STRUCTURAL",)
    assert before[0].completed_session_dollar_volume == 10_500.0
    assert before[1].structural_eligible is False
    assert before[1].reason_codes == ("UNSUPPORTED_SECURITY_TYPE",)

    future_bar = _bar(
        "security-a",
        "AAA",
        date(2021, 6, 2),
        usable=datetime(2021, 6, 2, 20, 1, tzinfo=UTC),
        close=1000.0,
    )
    future_identity = _identity(
        "security-future",
        "AAA",
        date(2026, 1, 2),
        None,
        usable=datetime(2026, 1, 2, 22, 0, tzinfo=UTC),
    )
    after = build_structural_universe_view(
        identity_rows=(common, fund, future_identity),
        bars=(common_bar, fund_bar, future_bar),
        session=session,
        signal_cutoff=cutoff,
    )
    assert after == before


def test_canonical_bar_loader_requires_clean_raw_source_admission() -> None:
    source_identifier = "fixture-raw-source"
    admission = _admit(
        _descriptor(
            "RAW_DAILY_OHLCV",
            source_identifier=source_identifier,
        )
    )
    when = datetime(2021, 6, 1, 20, 1, tzinfo=UTC)
    availability = AvailabilityStamp(
        effective_time=when,
        published_time=when,
        received_time=when,
        usable_time=when,
        source_revision="1",
        source_identifier=source_identifier,
        source_snapshot_id=_hash("canonical-bar-snapshot"),
    )
    bar = CausalDailyBar.create(
        stable_security_id="security-a",
        session=date(2021, 6, 1),
        open=10.0,
        high=11.0,
        low=9.5,
        close=10.5,
        volume=1000,
        trade_count=None,
        vwap=None,
        availability=availability,
        source_release_id=_hash("raw-source-release"),
        identity_snapshot_id=_hash("identity-snapshot"),
        adjustment_state="RAW_OBSERVED",
        raw_source_bar_id=None,
        corporate_action_ids=(),
        quality_flags=(),
        evidence_state="PIT_CONFIRMED",
    )
    assert require_source_admitted_bar(bar, admission) is bar

    legacy = _admit(
        _descriptor(
            "RAW_DAILY_OHLCV",
            evidence_class="LEGACY_DISCOVERY",
            source_identifier=source_identifier,
        )
    )
    with pytest.raises(ContractError, match="qualified raw-source admission"):
        require_source_admitted_bar(bar, legacy)


def test_new_source_contract_artifacts_contain_no_outcome_unlock() -> None:
    for relative in (
        "config/historical_source_contract_v1.json",
        "config/historical_source_admission_policy_v1.json",
        "config/historical_source_acquisition_requirements_v1.json",
        "config/legacy_historical_data_quarantine_v1.json",
        "config/v1_price_volume_research_scope.json",
    ):
        payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        serialized = json.dumps(payload, sort_keys=True)
        assert '"real_outcome_access": true' not in serialized
        assert '"this_record_authorizes_bulk_download": true' not in serialized
