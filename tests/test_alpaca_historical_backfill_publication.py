from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.cli import plan_alpaca_historical_backfill_publication as cli
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import IntegrityError
from us_stocks_swing_model_v2.providers.alpaca_historical_backfill import (
    SOURCE_NAME,
    _retained_snapshot_inventory,
    build_historical_backfill_complete_corpus,
    build_historical_backfill_fixture_plan,
)
from us_stocks_swing_model_v2.providers.alpaca_historical_backfill_publication import (
    CODE_CLOSURE_PATHS,
    DATASET,
    INPUT_QUALITY_STATE,
    MODE,
    QUALITY_STATE,
    build_historical_backfill_publication_plan_from_corpus,
    build_historical_backfill_release,
    load_historical_backfill_publication_policy,
    publish_historical_backfill_fixture,
    publication_plan_summary,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    AsReceivedSnapshotStore,
    NetworkAcquisitionRegistry,
)


REPO = Path(__file__).resolve().parents[1]
CREATED_AT = "2026-07-31T20:00:00Z"
REQUESTED_AT = datetime(2026, 7, 30, 4, 20, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 7, 30, 4, 21, tzinfo=timezone.utc)


def _registry() -> NetworkAcquisitionRegistry:
    return NetworkAcquisitionRegistry.load(
        REPO / "config/alpaca_historical_backfill_network_registry.json",
        allowed_root=REPO,
    )


def _fixture(tmp_path: Path):
    symbol_digest = sha256_bytes(canonical_json_bytes(["AAA"]))
    empty_digest = sha256_bytes(canonical_json_bytes([]))
    plan = build_historical_backfill_fixture_plan(
        repo_root=REPO,
        identity_rows=[
            {
                "symbol": "AAA",
                "asset_id": "asset-AAA",
                "security_type": "STOCK",
                "eligible": True,
                "active": True,
                "membership_present": True,
                "identity_snapshot_id": (
                    "679c22119b9e3a9cdf19424ab9eccef5dae85bb5cb7be70502bdc597d2932df6"
                ),
            }
        ],
        rehabilitated_symbols=[],
        sessions=[date(2016, 1, 4)],
        expected_selection={
            "eligible": True,
            "active": True,
            "membership_present": True,
            "security_types": ["ETF", "STOCK"],
            "expected_eligible_count": 1,
            "expected_eligible_symbols_sha256": symbol_digest,
            "expected_overlap_count": 0,
            "expected_overlap_symbols_sha256": empty_digest,
            "expected_missing_count": 1,
            "expected_missing_symbols_sha256": symbol_digest,
            "expected_legacy_only_count": 0,
            "expected_legacy_only_symbols_sha256": empty_digest,
        },
    )
    unit = plan["request_units"][0]
    eastern = ZoneInfo("America/New_York")
    event_at = datetime.combine(date(2016, 1, 4), time.min, eastern).astimezone(
        timezone.utc
    )
    payload = {
        "bars": {
            "AAA": [
                {
                    "t": event_at.isoformat().replace("+00:00", "Z"),
                    "o": 10.0,
                    "h": 11.0,
                    "l": 9.0,
                    "c": 10.5,
                    "v": 100,
                    "n": 10,
                    "vw": 10.25,
                }
            ]
        },
        "next_page_token": None,
    }
    allowed = tmp_path / "data"
    allowed.mkdir(parents=True)
    store = AsReceivedSnapshotStore(allowed / "snapshots", allowed_root=allowed)
    store.land(
        source=SOURCE_NAME,
        url=unit["network_request_plan"]["initial_url"],
        http_status=200,
        raw=canonical_json_bytes(payload),
        headers={"content-type": "application/json"},
        retrieved_at=RETRIEVED_AT,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="historical-backfill-release-builder",
            scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
        ),
        max_bytes=16777216,
        requested_at=REQUESTED_AT,
        request_plan_id=unit["network_request_plan"]["plan_id"],
    )
    inventory = _retained_snapshot_inventory(store)
    calendar = [date(2016, 1, 4)]
    complete = build_historical_backfill_complete_corpus(
        backfill_plan=plan,
        snapshot_store=store,
        calendar_sessions=calendar,
        registry=_registry(),
        synthetic=True,
        _inventory=inventory,
    )
    policy, policy_id = load_historical_backfill_publication_policy(REPO)
    policy = json.loads(json.dumps(policy))
    policy["completeness_contract"].update(
        {
            "expected_group_count": 1,
            "expected_unit_count": 1,
            "expected_symbol_count": 1,
            "expected_window_count": 1,
        }
    )
    policy["release_contract"]["expected_shard_count"] = 1
    build = build_historical_backfill_release(
        backfill_plan=plan,
        complete_corpus=complete,
        snapshot_inventory=inventory,
        calendar_sessions=calendar,
        registry=_registry(),
        policy=policy,
        publication_policy_id=policy_id,
        created_at=CREATED_AT,
        code_hash="b" * 64,
        config_hash="c" * 64,
        environment_hash="d" * 64,
        synthetic=True,
    )
    return policy, policy_id, build


def test_checked_in_policy_has_builder_but_is_non_authorizing() -> None:
    policy, policy_id = load_historical_backfill_publication_policy(REPO)

    assert policy_id == sha256_bytes(canonical_json_bytes(policy))
    assert policy["mode"] == MODE
    assert policy["release_contract"]["dataset"] == DATASET
    assert policy["release_contract"]["quality_state"] == QUALITY_STATE
    assert policy["release_contract"]["input_quality_state"] == INPUT_QUALITY_STATE
    assert policy["implementation"]["release_builder_implemented"] is True
    assert policy["implementation"]["publication_execution_implemented"] is True
    assert all(value is False for value in policy["authorities"].values())
    assert "src/us_stocks_swing_model_v2/canonical/alpaca.py" in CODE_CLOSURE_PATHS
    assert "src/us_stocks_swing_model_v2/canonical/parquet.py" in CODE_CLOSURE_PATHS


def test_release_builder_is_deterministic_and_copies_exact_evidence(
    tmp_path: Path,
) -> None:
    policy, policy_id, first = _fixture(tmp_path)
    _policy, _policy_id, second = _fixture(tmp_path / "second")

    assert first.manifest.release_id == second.manifest.release_id
    assert first.manifest.role == "qualification_evidence_only"
    assert first.manifest.quality_state == "QUALIFICATION_EVIDENCE"
    assert first.manifest.row_count == 1
    assert first.manifest.event_start == "2016-01-04"
    assert len(first.shard_census) == 1
    assert len(first.copied_files) == 3
    assert all(
        path.startswith("source_snapshots/")
        and len(path.split("/")[1]) == 20
        for path, _source in first.copied_files
    )
    assert first.evidence_manifest_id == second.evidence_manifest_id
    shard = dict(first.generated_files)["bars/year=2016.parquet"]
    table = pq.read_table(pa.BufferReader(shard))
    assert table.to_pylist()[0]["provider_symbol"] == "AAA"
    assert table.to_pylist()[0]["point_in_time_safe"] is False

    plan = build_historical_backfill_publication_plan_from_corpus(
        release_build=first,
        policy=policy,
        publication_policy_id=policy_id,
        accepted_root=(tmp_path / "accepted").resolve(),
        work_root=(tmp_path / "work").resolve(),
        created_at=CREATED_AT,
        code_closure_sha256="b" * 64,
        config_closure_sha256="c" * 64,
        environment_id="d" * 64,
    )
    assert plan["prospective_release"]["release_id"] == first.manifest.release_id
    assert plan["prospective_release"]["shard_count"] == 1
    assert publication_plan_summary(plan)["publication_plan_id"] == plan[
        "publication_plan_id"
    ]


def test_release_builder_rejects_tampered_completeness_identity(tmp_path: Path) -> None:
    policy, policy_id, build = _fixture(tmp_path)
    build.complete_corpus["raw_bytes"] = 1

    with pytest.raises(IntegrityError, match="complete-corpus ID differs"):
        build_historical_backfill_publication_plan_from_corpus(
            release_build=build,
            policy=policy,
            publication_policy_id=policy_id,
            accepted_root=(tmp_path / "accepted").resolve(),
            work_root=(tmp_path / "work").resolve(),
            created_at=CREATED_AT,
            code_closure_sha256="b" * 64,
            config_closure_sha256="c" * 64,
            environment_id="d" * 64,
        )


def test_cli_emits_exact_release_identity_without_execution_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy, policy_id, build = _fixture(tmp_path)
    plan = build_historical_backfill_publication_plan_from_corpus(
        release_build=build,
        policy=policy,
        publication_policy_id=policy_id,
        accepted_root=(REPO / "data/vault/accepted").resolve(),
        work_root=(REPO / "data/w/alpaca_historical_backfill_publication").resolve(),
        created_at=CREATED_AT,
        code_closure_sha256="b" * 64,
        config_closure_sha256="c" * 64,
        environment_id="d" * 64,
    )
    monkeypatch.setattr(
        cli,
        "build_historical_backfill_publication_plan",
        lambda **_kwargs: plan,
    )

    assert cli.main(["--created-at", CREATED_AT]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "PLAN_ONLY_NO_NETWORK_NO_WRITES"
    assert output["publication_plan"]["prospective_release"]["release_id"] == (
        build.manifest.release_id
    )
    assert output["release_builder_implemented"] is True
    assert output["publication_implemented"] is True


def test_synthetic_publication_is_atomic_and_rejects_stage_reuse(tmp_path: Path) -> None:
    policy, policy_id, build = _fixture(tmp_path)
    # Keep this synthetic publication below the pytest run root but avoid its
    # long per-test path, which would mask the publisher contract on Windows.
    short_root = tmp_path.parent / "backfill-publication"
    accepted = (short_root / "accepted").resolve()
    work = (short_root / "work").resolve()
    plan = build_historical_backfill_publication_plan_from_corpus(
        release_build=build,
        policy=policy,
        publication_policy_id=policy_id,
        accepted_root=accepted,
        work_root=work,
        created_at=CREATED_AT,
        code_closure_sha256="b" * 64,
        config_closure_sha256="c" * 64,
        environment_id="d" * 64,
    )

    published = publish_historical_backfill_fixture(
        release_build=build,
        plan=plan,
        accepted_root=accepted,
        work_root=work,
    )

    assert published.release_id == build.manifest.release_id
    assert (published.release_directory / "bars/year=2016.parquet").is_file()
    assert (published.release_directory / "source_snapshots").is_dir()
    with pytest.raises(IntegrityError, match="stage already exists"):
        publish_historical_backfill_fixture(
            release_build=build,
            plan=plan,
            accepted_root=accepted,
            work_root=work,
        )
