from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from us_stocks_swing_model_v2.alpaca_archive_rehabilitation import (
    ArchiveExpectations,
)
import us_stocks_swing_model_v2.alpaca_archive_rehabilitation_publisher as publisher
from us_stocks_swing_model_v2.alpaca_archive_rehabilitation_publisher import (
    EVIDENCE_CLASS,
    QUALITY_STATE,
    ROLE,
    SYNTHETIC_PUBLICATION_SCOPE,
    SYNTHETIC_STATUS,
    build_rehabilitation_candidate,
    build_synthetic_rehabilitation_publication_plan,
    load_rehabilitation_publication_policy,
    publish_rehabilitation_fixture,
    verify_rehabilitation_release,
)
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.cli.publish_alpaca_archive_rehabilitation import (
    main as publication_main,
)
import us_stocks_swing_model_v2.cli.publish_alpaca_archive_rehabilitation as publication_cli
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError


REPO = Path(__file__).resolve().parents[1]
CREATED_AT = "2026-07-30T20:00:00Z"


def _bar(session: str, close: float) -> dict[str, object]:
    return {
        "c": close,
        "h": close + 1.0,
        "l": close - 1.0,
        "n": 10,
        "o": close - 0.5,
        "t": f"{session}T04:00:00Z",
        "v": 1000,
        "vw": close,
    }


def _write_page(
    path: Path,
    *,
    bars: dict[str, list[dict[str, object]]],
    next_page_token: str | None,
) -> tuple[str, int]:
    raw = canonical_json_bytes(
        {"bars": bars, "next_page_token": next_page_token}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _archive_fixture(
    tmp_path: Path,
    *,
    zero_vwap_with_activity: bool = False,
) -> tuple[Path, ArchiveExpectations]:
    root = tmp_path / "archive"
    page_root = root / "native" / "bars" / "raw" / "chunk_00001"
    page_one = page_root / "page_00001.json.gz"
    page_two = page_root / "page_00002.json.gz"
    first_aapl = _bar("2026-07-28", 100.0)
    if zero_vwap_with_activity:
        first_aapl["vw"] = 0
    else:
        first_aapl.update({"v": 0, "n": 0, "vw": 0})
    identity_one = _write_page(
        page_one,
        bars={
            "AAPL": [first_aapl],
            "SPY": [_bar("2026-07-28", 500.0)],
        },
        next_page_token="page-two",
    )
    identity_two = _write_page(
        page_two,
        bars={
            "AAPL": [_bar("2026-07-29", 101.0)],
            "SPY": [_bar("2026-07-29", 501.0)],
        },
        next_page_token=None,
    )
    page_bindings = [
        {
            "path": str(page_one),
            "sha256": identity_one[0],
            "uncompressed_bytes": identity_one[1],
        },
        {
            "path": str(page_two),
            "sha256": identity_two[0],
            "uncompressed_bytes": identity_two[1],
        },
    ]
    provenance_root = root / "bars" / "raw"
    provenance_root.mkdir(parents=True)
    for symbol in ("AAPL", "SPY"):
        (provenance_root / f"{symbol}.parquet.provenance.json").write_text(
            json.dumps(
                {
                    "adjustment": "raw",
                    "canonical_symbol": symbol,
                    "feed": "sip",
                    "max_date": "20260729",
                    "min_date": "20260728",
                    "native_pages": page_bindings,
                    "provider_symbol": symbol,
                    "request_end_date": "2026-07-30",
                    "request_start_date": "2026-07-28",
                    "row_count": 2,
                    "source": "alpaca_sip_v1",
                    "source_route_name": "historical_stock_bars",
                    "timeframe": "1Day",
                    "validation_passed": False,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    expectations = ArchiveExpectations(
        source="alpaca_sip_v1",
        source_route_name="historical_stock_bars",
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        request_start_date="2026-07-28",
        request_end_date="2026-07-30",
        symbol_count=2,
        page_count=2,
        chunk_count=1,
        row_count=4,
        event_start="2026-07-28",
        event_end="2026-07-29",
        compressed_bytes=page_one.stat().st_size + page_two.stat().st_size,
        uncompressed_bytes=identity_one[1] + identity_two[1],
    )
    return root, expectations


def _candidate(tmp_path: Path):
    archive, expectations = _archive_fixture(tmp_path)
    return build_rehabilitation_candidate(
        archive,
        expectations=expectations,
        metadata_evidence_files=(),
        assessment_id="1" * 64,
        rehabilitation_policy_id="2" * 64,
        hfdl_retirement_policy_id="3" * 64,
        evidence_boundary={
            "input_is_original_http_response_bytes": False,
            "input_is_canonicalized_provider_json_payload": True,
            "per_page_retrieval_times_available": False,
            "historical_membership_point_in_time_safe": False,
        },
    )


def _permit(candidate_id: str) -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id=candidate_id,
        scope=SYNTHETIC_PUBLICATION_SCOPE,
    )


def test_checked_in_publication_policy_is_exact_and_non_authorizing() -> None:
    policy, policy_id = load_rehabilitation_publication_policy(REPO)

    assert policy_id == sha256_bytes(canonical_json_bytes(policy))
    assert policy["assessment_binding"]["plan_id"] == (
        "cbff5e58eb3ccd3220aec14e0ed5ad7608ab411c57a9ff1b028a0fb7c5452c7c"
    )
    assert policy["release_contract"]["role"] == ROLE
    assert policy["release_contract"]["quality_state"] == QUALITY_STATE
    assert policy["release_contract"]["copy_exact_page_count"] == 198
    assert policy["release_contract"]["copy_legacy_derived_parquet"] is False
    assert policy["release_contract"]["active_source_eligible"] is False
    assert all(value is False for value in policy["authorities"].values())


def test_candidate_is_deterministic_no_write_and_keeps_causal_caveats(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    before = {
        path.relative_to(candidate.archive_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in candidate.archive_root.rglob("*")
        if path.is_file()
    }
    rebuilt = build_rehabilitation_candidate(
        candidate.archive_root,
        expectations=ArchiveExpectations(
            source="alpaca_sip_v1",
            source_route_name="historical_stock_bars",
            feed="sip",
            timeframe="1Day",
            adjustment="raw",
            request_start_date="2026-07-28",
            request_end_date="2026-07-30",
            symbol_count=2,
            page_count=2,
            chunk_count=1,
            row_count=4,
            event_start="2026-07-28",
            event_end="2026-07-29",
            compressed_bytes=sum(page.compressed_size for page in candidate.pages),
            uncompressed_bytes=sum(
                page.uncompressed_size for page in candidate.pages
            ),
        ),
        metadata_evidence_files=(),
        assessment_id="1" * 64,
        rehabilitation_policy_id="2" * 64,
        hfdl_retirement_policy_id="3" * 64,
        evidence_boundary=candidate.evidence_boundary,
    )

    assert rebuilt.candidate_id == candidate.candidate_id
    assert rebuilt.bars_bytes == candidate.bars_bytes
    assert candidate.row_count == 4
    assert candidate.normalized_zero_activity_vwap_rows == 1
    assert candidate.table["asset_id"].null_count == 4
    assert candidate.table["available_at"].null_count == 4
    assert candidate.table["vwap"].null_count == 1
    assert set(candidate.table["evidence_class"].to_pylist()) == {EVIDENCE_CLASS}
    assert set(candidate.table["quality_state"].to_pylist()) == {QUALITY_STATE}
    assert set(candidate.table["historical_proxy"].to_pylist()) == {True}
    assert set(candidate.table["point_in_time_safe"].to_pylist()) == {False}
    after = {
        path.relative_to(candidate.archive_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in candidate.archive_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_candidate_rejects_zero_vwap_when_trading_activity_exists(
    tmp_path: Path,
) -> None:
    archive, expectations = _archive_fixture(
        tmp_path,
        zero_vwap_with_activity=True,
    )
    with pytest.raises(ContractError, match="OHLCV invariants"):
        build_rehabilitation_candidate(
            archive,
            expectations=expectations,
            metadata_evidence_files=(),
            assessment_id="1" * 64,
            rehabilitation_policy_id="2" * 64,
            hfdl_retirement_policy_id="3" * 64,
            evidence_boundary={
                "input_is_original_http_response_bytes": False,
                "input_is_canonicalized_provider_json_payload": True,
                "per_page_retrieval_times_available": False,
                "historical_membership_point_in_time_safe": False,
            },
        )


def test_synthetic_publication_is_atomic_idempotent_and_caveated(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    permit = _permit(candidate.candidate_id)
    accepted = tmp_path / "accepted"
    work = tmp_path / "work"
    plan = build_synthetic_rehabilitation_publication_plan(
        candidate=candidate,
        accepted_root=accepted,
        work_root=work,
        created_at=CREATED_AT,
        permit=permit,
        publication_policy_id="4" * 64,
    )

    first = publish_rehabilitation_fixture(
        candidate=candidate,
        plan=plan,
        accepted_root=accepted,
        work_root=work,
        fixture_root=tmp_path,
        permit=permit,
    )
    second = publish_rehabilitation_fixture(
        candidate=candidate,
        plan=plan,
        accepted_root=accepted,
        work_root=work,
        fixture_root=tmp_path,
        permit=permit,
    )

    assert first.release_directory == second.release_directory
    assert first.release_id == second.release_id
    assert first.release_directory == accepted / publisher.DATASET / first.release_id
    receipt = verify_rehabilitation_release(
        first.release_directory,
        accepted_root=accepted,
        expected_plan_id=plan["publication_plan_id"],
        synthetic=True,
    )
    assert receipt["status"] == SYNTHETIC_STATUS
    assert receipt["outputs"]["normalized_zero_activity_vwap_rows"] == 1
    assert receipt["authorities"]["legacy_discovery_publication"] is False
    assert all(
        receipt["authorities"][name] is False
        for name in (
            "active_source",
            "eligible_universe",
            "features_or_outcomes",
            "training_or_evaluation",
            "research",
            "hfdl",
        )
    )
    manifest = json.loads(
        (first.release_directory / "release_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["role"] == ROLE
    assert manifest["quality_state"] == QUALITY_STATE
    assert manifest["row_count"] == 4
    table = pq.read_table(first.release_directory / publisher.BARS_PATH)
    assert table.num_rows == 4
    assert not any(
        "bars/raw/" in entry["path"] and entry["path"].endswith(".parquet")
        for entry in manifest["files"]
    )
    for page in candidate.pages:
        copied = first.release_directory / page.output_relative
        assert copied.read_bytes() == page.source_path.read_bytes()
        assert not copied.samefile(page.source_path)


def test_publication_rejects_wrong_permit_tampering_and_unexpected_stage(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    permit = _permit(candidate.candidate_id)
    accepted = tmp_path / "accepted"
    work = tmp_path / "work"
    plan = build_synthetic_rehabilitation_publication_plan(
        candidate=candidate,
        accepted_root=accepted,
        work_root=work,
        created_at=CREATED_AT,
        permit=permit,
        publication_policy_id="4" * 64,
    )
    wrong = SyntheticOnlyPermit.create(
        fixture_id="wrong",
        scope=SYNTHETIC_PUBLICATION_SCOPE,
    )
    with pytest.raises(ContractError, match="differs from its permit"):
        publish_rehabilitation_fixture(
            candidate=candidate,
            plan=plan,
            accepted_root=accepted,
            work_root=work,
            fixture_root=tmp_path,
            permit=wrong,
        )

    page = candidate.pages[0]
    original = page.source_path.read_bytes()
    page.source_path.write_bytes(original + b"tamper")
    with pytest.raises(IntegrityError, match="source page changed"):
        publish_rehabilitation_fixture(
            candidate=candidate,
            plan=plan,
            accepted_root=accepted,
            work_root=work,
            fixture_root=tmp_path,
            permit=permit,
        )
    page.source_path.write_bytes(original)

    stage = work / plan["publication_plan_id"] / "stage"
    stage.mkdir(parents=True)
    (stage / "unexpected.txt").write_text("no", encoding="utf-8")
    with pytest.raises(IntegrityError, match="unexpected entries"):
        publish_rehabilitation_fixture(
            candidate=candidate,
            plan=plan,
            accepted_root=accepted,
            work_root=work,
            fixture_root=tmp_path,
            permit=permit,
        )


def test_release_verifier_rejects_receipt_tampering(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    permit = _permit(candidate.candidate_id)
    accepted = tmp_path / "accepted"
    work = tmp_path / "work"
    plan = build_synthetic_rehabilitation_publication_plan(
        candidate=candidate,
        accepted_root=accepted,
        work_root=work,
        created_at=CREATED_AT,
        permit=permit,
        publication_policy_id="4" * 64,
    )
    result = publish_rehabilitation_fixture(
        candidate=candidate,
        plan=plan,
        accepted_root=accepted,
        work_root=work,
        fixture_root=tmp_path,
        permit=permit,
    )
    receipt_path = result.release_directory / publisher.RECEIPT_PATH
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["authorities"]["research"] = True
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(IntegrityError, match="payload hash mismatch"):
        verify_rehabilitation_release(
            result.release_directory,
            accepted_root=accepted,
            synthetic=True,
        )


def test_cli_defaults_to_plan_only_and_execute_fails_before_planning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = {
        "publication_plan_id": "a" * 64,
        "prospective_release": {"release_id": "b" * 64},
    }
    monkeypatch.setattr(
        publication_cli,
        "build_rehabilitation_publication_plan",
        lambda **kwargs: plan,
    )
    monkeypatch.setattr(
        publication_cli,
        "publish_rehabilitation_release",
        lambda **kwargs: pytest.fail("plan-only CLI attempted publication"),
    )
    assert publication_main(["--created-at", CREATED_AT]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "PLAN_ONLY_NO_WRITES"
    assert output["publication_authorized"] is False
    assert output["network_calls"] == 0

    monkeypatch.delenv(publisher.PUBLICATION_CONFIRMATION_TOKEN, raising=False)
    with pytest.raises(PermissionError, match=publisher.PUBLICATION_CONFIRMATION_TOKEN):
        publication_main(
            [
                "--created-at",
                CREATED_AT,
                "--execute",
                "--approved-plan-id",
                "a" * 64,
            ]
        )


def test_publisher_has_no_network_or_credential_transport() -> None:
    source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "alpaca_archive_rehabilitation_publisher.py"
    ).read_text(encoding="utf-8")
    cli_source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "cli"
        / "publish_alpaca_archive_rehabilitation.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "urllib",
        "requests.",
        "httpx",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "api.env",
        "--execute-network",
    ):
        assert forbidden not in source
        assert forbidden not in cli_source
