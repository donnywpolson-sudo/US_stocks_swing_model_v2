from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes, sha256_file
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.providers.nasdaq import (
    NASDAQ_TRADED_URL,
    NasdaqCompletenessPolicy,
)
from us_stocks_swing_model_v2.providers.nasdaq_bootstrap import (
    PASS_STATUS,
    SYNTHETIC_PASS_STATUS,
    NasdaqBootstrapPolicy,
    load_nasdaq_bootstrap_policy,
    verify_nasdaq_bootstrap_pair,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    AsReceivedSnapshotStore,
    NetworkAcquisitionRegistry,
)


REPO = Path(__file__).resolve().parents[1]
HEADER = (
    "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|"
    "Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n"
)


def _snapshot_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="nasdaq-bootstrap-snapshots",
        scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
    )


def _completeness(
    *,
    maximum_drop_fraction: float = 1.0,
    maximum_count_change_fraction: float = 1.0,
) -> NasdaqCompletenessPolicy:
    return replace(
        NasdaqCompletenessPolicy.synthetic_fixture(
            permit=SyntheticOnlyPermit.create(
                fixture_id="nasdaq-bootstrap-completeness",
                scope="NASDAQ_COMPLETENESS_FIXTURE",
            )
        ),
        maximum_drop_fraction=maximum_drop_fraction,
        maximum_count_change_fraction=maximum_count_change_fraction,
    )


def _raw(symbols: tuple[str, ...], file_time: str) -> bytes:
    rows = "".join(
        f"Y|{symbol}|{symbol} COMMON STOCK|N|Q|N|100|N|N|{symbol}|{symbol}|N\n"
        for symbol in symbols
    )
    return f"{HEADER}{rows}File Creation Time: {file_time}|||||\n".encode()


def _pair(
    tmp_path: Path,
    *,
    symbols_a: tuple[str, ...] = ("AAA", "BBB"),
    symbols_b: tuple[str, ...] = ("AAA", "BBB", "CCC"),
    file_time_a: str = "0715202601:30",
    file_time_b: str = "0716202601:30",
    retrieved_a: datetime = datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
    retrieved_b: datetime = datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    snapshot_a = store.land(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=_raw(symbols_a, file_time_a),
        headers={"content-type": "text/plain"},
        retrieved_at=retrieved_a,
        synthetic_permit=_snapshot_permit(),
    )
    snapshot_b = store.land(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=_raw(symbols_b, file_time_b),
        headers={"content-type": "text/plain"},
        retrieved_at=retrieved_b,
        synthetic_permit=_snapshot_permit(),
    )
    return snapshot_a, snapshot_b


def test_checked_in_bootstrap_policy_binds_fresh_a_and_preserved_receipt() -> None:
    preserved = REPO / "config" / "nasdaq_qualification_receipt.json"
    before = preserved.read_bytes()
    policy = load_nasdaq_bootstrap_policy(REPO)
    assert policy.snapshot_a_id == (
        "b47b40cf912eccc49260d091c92d2435eaf23c630aac78d23a1ac44182df2e7b"
    )
    assert policy.snapshot_a_raw_sha256 == (
        "4ad35c455e05d653d3dc62d67fdb9b4692bdf12b9be8521289f66405447a7fba"
    )
    assert policy.preserved_receipt_id == (
        "3163ace9f5a71e403f1c030e1d06ef8bc42ea77cb03c71a1b63436882d87feca"
    )
    assert policy.preserved_record_count == 13050
    assert policy.preserved_receipt_file_sha256 == sha256_file(preserved)
    assert policy.network_registry_id == (
        "2f0272f1bb0a3e633bdb30c139ea1eb357bb4fee8e29d5a281cb62c47808e68f"
    )
    current_registry = NetworkAcquisitionRegistry.load(
        REPO / "config/network_acquisition_registry.json",
        allowed_root=REPO / "config",
    )
    assert current_registry.registry_id != policy.network_registry_id
    assert preserved.read_bytes() == before
    payload = json.loads(
        (REPO / "config" / "nasdaq_bootstrap_policy.json").read_text(
            encoding="utf-8"
        )
    )
    policy_id = payload.pop("policy_id")
    assert policy_id == sha256_bytes(canonical_json_bytes(payload))


def test_synthetic_pair_proves_mechanics_without_trust_or_activation(
    tmp_path: Path,
) -> None:
    snapshot_a, snapshot_b = _pair(tmp_path)
    policy = NasdaqBootstrapPolicy.synthetic_fixture(
        snapshot_a=snapshot_a,
        completeness=_completeness(),
    )
    assessment = verify_nasdaq_bootstrap_pair(
        snapshot_a,
        snapshot_b,
        policy=policy,
    )
    assessment_id = assessment.pop("assessment_id")
    assert assessment_id == sha256_bytes(canonical_json_bytes(assessment))
    assert assessment["status"] == SYNTHETIC_PASS_STATUS
    assert assessment["status"] != PASS_STATUS
    assert assessment["provenance"] == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
    assert assessment["baseline_candidate"]["active"] is False
    assert assessment["authorities"] == {
        "network_calls": False,
        "receipt_publication": False,
        "source_activation": False,
        "historical_relabel": False,
    }
    assert assessment["preserved_historical_comparison"]["record_count"] is None


def test_pair_requires_distinct_later_capture(tmp_path: Path) -> None:
    snapshot_a, snapshot_b = _pair(tmp_path)
    policy = NasdaqBootstrapPolicy.synthetic_fixture(
        snapshot_a=snapshot_a,
        completeness=_completeness(),
    )
    with pytest.raises(ContractError, match="distinct snapshot ID"):
        verify_nasdaq_bootstrap_pair(snapshot_a, snapshot_a, policy=policy)

    same_creation_a, same_creation_b = _pair(
        tmp_path / "same-creation",
        file_time_b="0715202601:30",
    )
    same_creation_policy = NasdaqBootstrapPolicy.synthetic_fixture(
        snapshot_a=same_creation_a,
        completeness=_completeness(),
    )
    with pytest.raises(ContractError, match="file-creation time must be later"):
        verify_nasdaq_bootstrap_pair(
            same_creation_a,
            same_creation_b,
            policy=same_creation_policy,
        )

    late_a, early_b = _pair(
        tmp_path / "early-retrieval",
        retrieved_b=datetime(2026, 7, 15, 11, tzinfo=timezone.utc),
    )
    early_policy = NasdaqBootstrapPolicy.synthetic_fixture(
        snapshot_a=late_a,
        completeness=_completeness(),
    )
    with pytest.raises(ContractError, match="retrieval must be later"):
        verify_nasdaq_bootstrap_pair(late_a, early_b, policy=early_policy)


def test_pair_applies_a_to_b_count_limits_not_preserved_old_count(
    tmp_path: Path,
) -> None:
    snapshot_a, snapshot_b = _pair(
        tmp_path,
        symbols_a=("AAA", "BBB", "CCC", "DDD"),
        symbols_b=("AAA",),
    )
    policy = NasdaqBootstrapPolicy.synthetic_fixture(
        snapshot_a=snapshot_a,
        completeness=_completeness(
            maximum_drop_fraction=0.10,
            maximum_count_change_fraction=0.25,
        ),
    )
    with pytest.raises(ContractError, match="count drop"):
        verify_nasdaq_bootstrap_pair(
            snapshot_a,
            snapshot_b,
            policy=policy,
        )
