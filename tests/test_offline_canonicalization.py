from __future__ import annotations

from dataclasses import replace
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import us_stocks_swing_model_v2.canonical.alpaca as alpaca_module
import us_stocks_swing_model_v2.canonical.hfdl as hfdl_module
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.canonical.alpaca import (
    AlpacaNativeManifest,
    NativePageSpec,
    NativeRequestSpec,
    SYNTHETIC_QUALIFICATION_PUBLICATION_SCOPE,
    _accept_native_bar,
    materialize_qualification_release,
    qualification_publication_binding_id,
    reparse_native_raw,
)
from us_stocks_swing_model_v2.canonical.hfdl import (
    HFDL_NATIVE_SCHEMA,
    HFDL_SIDECAR_EXTENSION_POLICY,
    load_validated_hfdl_sidecar,
    validate_and_tag_hfdl,
    write_tagged_hfdl_legacy,
    write_tagged_hfdl_legacy_epochs,
)
from us_stocks_swing_model_v2.common import sha256_file
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError


def _hfdl_fixture(root: Path) -> tuple[Path, Path]:
    parquet = root / "ABC.parquet"
    table = pa.Table.from_pydict(
        {
            "ticker": ["ABC", "ABC"],
            "per": ["D", "D"],
            "date": ["20220303", "20220304"],
            "time": ["000000", "000000"],
            "open": [10.0, 11.0],
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.5, 11.5],
            "vol": [100, 200],
            "openint": [0, 0],
        },
        schema=HFDL_NATIVE_SCHEMA,
    )
    pq.write_table(table, parquet)
    sidecar = root / "ABC.parquet.provenance.json"
    sidecar.write_text(
        json.dumps(
            {
                "canonical_symbol": "ABC",
                "created_at_utc": "2026-07-15T00:00:00Z",
                "row_count": 2,
                "sha256": sha256_file(parquet),
                "timeframe": "daily",
                "validation_passed": True,
                "version": "clean",
                "source_limitations": [
                    "Universe is a fixed snapshot.",
                    "Pre-March 2022 and post-March 2022 feeds differ.",
                    "Clean files are source-adjusted.",
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return parquet, sidecar


def test_hfdl_validator_tags_feed_break_and_is_clean_room_deterministic(tmp_path: Path) -> None:
    parquet, sidecar = _hfdl_fixture(tmp_path)
    result = validate_and_tag_hfdl(parquet, sidecar)
    assert result.epoch_counts == {"hfdl_pitrading_consolidated": 1, "hfdl_iex_only": 1}
    assert result.table.column("point_in_time_safe").to_pylist() == [False, False]
    assert set(result.table.column("point_in_time_state").to_pylist()) == {"HISTORICAL_PROXY"}
    assert set(result.table.column("historical_availability_state").to_pylist()) == {
        "UNKNOWN_NOT_AS_RECEIVED"
    }
    assert "retrieved_at" not in result.table.column_names
    assert "source_retrieved_at" in result.table.column_names
    assert result.release_metadata()["point_in_time_state"] != "PIT_CONFIRMED"
    with pytest.raises(ContractError, match="separate releases"):
        write_tagged_hfdl_legacy(result, tmp_path / "pooled" / "bars.parquet")
    first = write_tagged_hfdl_legacy_epochs(result, tmp_path / "clean1")
    second = write_tagged_hfdl_legacy_epochs(result, tmp_path / "clean2")
    assert set(first) == {"hfdl_pitrading_consolidated", "hfdl_iex_only"}
    assert all(first[epoch].read_bytes() == second[epoch].read_bytes() for epoch in first)


def test_hfdl_sidecar_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    parquet, sidecar = _hfdl_fixture(tmp_path)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["sha256"] = "0" * 64
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IntegrityError, match="hash differs"):
        validate_and_tag_hfdl(parquet, sidecar)


def test_hfdl_sidecar_extensions_are_ignored_by_the_canonical_validator(
    tmp_path: Path,
) -> None:
    parquet, sidecar = _hfdl_fixture(tmp_path)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["vendor_extension"] = {
        "validation_passed": False,
        "row_count": "untrusted",
    }
    sidecar.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    validated = load_validated_hfdl_sidecar(sidecar)
    assert "vendor_extension" not in validated
    assert HFDL_SIDECAR_EXTENSION_POLICY == (
        "ALLOW_UNTRUSTED_IGNORED_EXTENSION_FIELDS"
    )
    assert validate_and_tag_hfdl(parquet, sidecar).row_count == 2


def test_hfdl_symbols_require_exact_uppercase_evidence(
    tmp_path: Path,
) -> None:
    sidecar_root = tmp_path / "sidecar"
    sidecar_root.mkdir()
    parquet, sidecar = _hfdl_fixture(sidecar_root)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["canonical_symbol"] = "abc"
    sidecar.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ContractError, match="exact uppercase"):
        validate_and_tag_hfdl(parquet, sidecar)

    row_root = tmp_path / "row"
    row_root.mkdir()
    parquet, sidecar = _hfdl_fixture(row_root)
    table = pq.read_table(parquet)
    rows = table.to_pylist()
    rows[0]["ticker"] = "abc"
    table = pa.Table.from_pylist(
        rows,
        schema=HFDL_NATIVE_SCHEMA,
    )
    pq.write_table(table, parquet)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["sha256"] = sha256_file(parquet)
    sidecar.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ContractError, match="identity/timeframe"):
        validate_and_tag_hfdl(parquet, sidecar)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("row_count", True),
        ("row_count", 2.0),
        ("row_count", "2"),
        ("canonical_symbol", 123),
        ("created_at_utc", "2026-07-14T17:00:00-07:00"),
        ("source_limitations", "fixed March 2022 source-adjusted"),
        ("source_limitations", ["fixed", 202203, "source-adjusted"]),
    ],
)
def test_public_hfdl_validator_rejects_non_exact_sidecar_evidence(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    root = tmp_path / f"{field}-{type(value).__name__}"
    root.mkdir()
    parquet, sidecar = _hfdl_fixture(root)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload[field] = value
    sidecar.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ContractError):
        validate_and_tag_hfdl(parquet, sidecar)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", 20220303),
        ("time", 0),
    ],
)
def test_public_hfdl_validator_rejects_non_string_native_time_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    parquet, sidecar = _hfdl_fixture(tmp_path)
    actual = pq.read_table(parquet)
    poisoned_rows = actual.to_pylist()
    poisoned_rows[0][field] = value

    class PoisonedTable:
        column_names = actual.column_names
        schema = actual.schema

        @staticmethod
        def to_pylist():
            return poisoned_rows

    monkeypatch.setattr(
        hfdl_module.pq,
        "read_table",
        lambda path: PoisonedTable(),
    )
    with pytest.raises(ContractError, match="identity/timeframe"):
        validate_and_tag_hfdl(parquet, sidecar)


def test_hfdl_duplicate_sessions_fail_closed_with_valid_sidecar(
    tmp_path: Path,
) -> None:
    parquet, sidecar = _hfdl_fixture(tmp_path)
    table = pq.read_table(parquet)
    duplicated = pa.concat_tables((table, table.slice(0, 1)))
    pq.write_table(duplicated, parquet)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["row_count"] = duplicated.num_rows
    payload["sha256"] = sha256_file(parquet)
    sidecar.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(ContractError, match="unique and strictly increasing"):
        validate_and_tag_hfdl(parquet, sidecar)


def _native_manifest(root: Path, *, duplicate: bool = False, broken_token: bool = False) -> AlpacaNativeManifest:
    root.mkdir(parents=True, exist_ok=True)
    page1_payload = {
        "bars": {
            "ABC": [
                {"o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 100, "n": 10, "vw": 10.2, "t": "2026-07-13T04:00:00Z"}
            ]
        },
        "next_page_token": "token-2",
    }
    second_session = "2026-07-13T04:00:00Z" if duplicate else "2026-07-14T04:00:00Z"
    page2_payload = {
        "bars": {
            "ABC": [
                {"o": 10.5, "h": 12.0, "l": 10.0, "c": 11.5, "v": 120, "n": 11, "vw": 11.2, "t": second_session}
            ]
        },
        "next_page_token": None,
    }
    specs = []
    for index, payload in enumerate((page1_payload, page2_payload), start=1):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        path = root / f"page{index}.json.gz"
        path.write_bytes(gzip.compress(raw, mtime=0))
        specs.append(
            NativePageSpec(
                path=path,
                sha256=sha256_file(path),
                uncompressed_bytes=len(raw),
                page_index=index,
                page_token_in=None if index == 1 else ("wrong" if broken_token else "token-2"),
                next_page_token_expected="token-2" if index == 1 else None,
            )
        )
    return AlpacaNativeManifest(
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        asof="legacy_default_mapping",
        retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        source_epoch="alpaca_sip_legacy_qualification_raw_v1",
        quality_state="FAIL",
        requests=(NativeRequestSpec("request-1", tuple(specs)),),
        symbol_to_asset_id={"ABC": "asset-abc"},
    )


def _native_manifest_with_integer_value(
    root: Path,
    *,
    field: str,
    value: object,
) -> AlpacaNativeManifest:
    manifest = _native_manifest(root)
    page = manifest.requests[0].pages[0]
    payload = json.loads(gzip.decompress(page.path.read_bytes()))
    payload["bars"]["ABC"][0][field] = value
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    page.path.write_bytes(gzip.compress(raw, mtime=0))
    changed_page = NativePageSpec(
        path=page.path,
        sha256=sha256_file(page.path),
        uncompressed_bytes=len(raw),
        page_index=page.page_index,
        page_token_in=page.page_token_in,
        next_page_token_expected=page.next_page_token_expected,
    )
    request = NativeRequestSpec(
        manifest.requests[0].request_id,
        (changed_page, *manifest.requests[0].pages[1:]),
    )
    return AlpacaNativeManifest(
        feed=manifest.feed,
        timeframe=manifest.timeframe,
        adjustment=manifest.adjustment,
        asof=manifest.asof,
        retrieved_at=manifest.retrieved_at,
        source_epoch=manifest.source_epoch,
        quality_state=manifest.quality_state,
        requests=(request,),
        symbol_to_asset_id=manifest.symbol_to_asset_id,
    )


def _native_manifest_with_timestamp(
    root: Path,
    value: object,
) -> AlpacaNativeManifest:
    return _native_manifest_with_integer_value(root, field="t", value=value)


def _native_manifest_with_symbol(
    root: Path,
    value: str,
) -> AlpacaNativeManifest:
    manifest = _native_manifest(root)
    page = manifest.requests[0].pages[0]
    payload = json.loads(gzip.decompress(page.path.read_bytes()))
    payload["bars"] = {value: payload["bars"]["ABC"]}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    page.path.write_bytes(gzip.compress(raw, mtime=0))
    changed_page = NativePageSpec(
        path=page.path,
        sha256=sha256_file(page.path),
        uncompressed_bytes=len(raw),
        page_index=page.page_index,
        page_token_in=page.page_token_in,
        next_page_token_expected=page.next_page_token_expected,
    )
    return AlpacaNativeManifest(
        feed=manifest.feed,
        timeframe=manifest.timeframe,
        adjustment=manifest.adjustment,
        asof=manifest.asof,
        retrieved_at=manifest.retrieved_at,
        source_epoch=manifest.source_epoch,
        quality_state=manifest.quality_state,
        requests=(
            NativeRequestSpec(
                manifest.requests[0].request_id,
                (changed_page, *manifest.requests[0].pages[1:]),
            ),
        ),
        symbol_to_asset_id=manifest.symbol_to_asset_id,
    )


def test_alpaca_native_reparse_and_release_are_deterministic_and_non_active(tmp_path: Path) -> None:
    result = reparse_native_raw(_native_manifest(tmp_path))
    assert result.row_count == 2
    assert result.quality_state == "FAIL"
    assert set(result.table.column("evidence_class").to_pylist()) == {"QUALIFICATION_EVIDENCE"}
    assert set(result.table.column("point_in_time_safe").to_pylist()) == {False}
    kwargs = dict(created_at="2026-07-15T00:00:00Z", code_hash="1" * 64, config_hash="2" * 64, environment_hash="3" * 64)

    def publication_kwargs(stage: Path, releases: Path) -> dict[str, object]:
        binding_id = qualification_publication_binding_id(
            result,
            staging_dir=stage,
            release_root=releases,
            **kwargs,
        )
        return {
            "publication_synthetic_permit": SyntheticOnlyPermit.create(
                fixture_id=binding_id,
                scope=SYNTHETIC_QUALIFICATION_PUBLICATION_SCOPE,
            ),
            "publication_allowed_root": tmp_path,
        }

    blocked_stage = tmp_path / "blocked-stage"
    with pytest.raises(ContractError, match="synthetic-only permit"):
        materialize_qualification_release(
            result,
            staging_dir=blocked_stage,
            release_root=tmp_path / "blocked-releases",
            **kwargs,
        )
    assert not blocked_stage.exists()

    first_stage = tmp_path / "stage1"
    first_releases = tmp_path / "releases1"
    first_manifest, first = materialize_qualification_release(
        result,
        staging_dir=first_stage,
        release_root=first_releases,
        **kwargs,
        **publication_kwargs(first_stage, first_releases),
    )
    second_stage = tmp_path / "stage2"
    second_releases = tmp_path / "releases2"
    second_manifest, second = materialize_qualification_release(
        result,
        staging_dir=second_stage,
        release_root=second_releases,
        **kwargs,
        **publication_kwargs(second_stage, second_releases),
    )
    assert first_manifest == second_manifest
    assert first_manifest.role == "qualification_evidence_only"
    assert first_manifest.quality_state == "FAIL"
    assert (first / "bars.parquet").read_bytes() == (second / "bars.parquet").read_bytes()


def test_alpaca_qualification_recovers_only_exact_recognized_partial_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = reparse_native_raw(_native_manifest(tmp_path / "native"))
    kwargs = {
        "created_at": "2026-07-15T00:00:00Z",
        "code_hash": "1" * 64,
        "config_hash": "2" * 64,
        "environment_hash": "3" * 64,
    }

    def publication_kwargs(stage: Path, releases: Path) -> dict[str, object]:
        binding_id = qualification_publication_binding_id(
            result,
            staging_dir=stage,
            release_root=releases,
            **kwargs,
        )
        return {
            "publication_synthetic_permit": SyntheticOnlyPermit.create(
                fixture_id=binding_id,
                scope=SYNTHETIC_QUALIFICATION_PUBLICATION_SCOPE,
            ),
            "publication_allowed_root": tmp_path,
        }

    stage = tmp_path / "recover-after-bars"
    releases = tmp_path / "releases-after-bars"
    original_atomic_write_new = alpaca_module.atomic_write_new

    def interrupt_before_summary(path: Path, payload: bytes) -> None:
        raise RuntimeError("synthetic interruption before summary commit")

    monkeypatch.setattr(alpaca_module, "atomic_write_new", interrupt_before_summary)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        materialize_qualification_release(
            result,
            staging_dir=stage,
            release_root=releases,
            **kwargs,
            **publication_kwargs(stage, releases),
        )
    assert set(path.name for path in stage.iterdir()) == {"bars.parquet"}
    monkeypatch.setattr(
        alpaca_module,
        "atomic_write_new",
        original_atomic_write_new,
    )
    manifest, published = materialize_qualification_release(
        result,
        staging_dir=stage,
        release_root=releases,
        **kwargs,
        **publication_kwargs(stage, releases),
    )
    assert published.name == manifest.release_id

    complete_stage = tmp_path / "recover-after-summary"
    complete_releases = tmp_path / "releases-after-summary"
    original_build_manifest = alpaca_module.build_manifest

    def interrupt_after_summary(*args: object, **call_kwargs: object) -> None:
        raise RuntimeError("synthetic interruption after summary commit")

    monkeypatch.setattr(alpaca_module, "build_manifest", interrupt_after_summary)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        materialize_qualification_release(
            result,
            staging_dir=complete_stage,
            release_root=complete_releases,
            **kwargs,
            **publication_kwargs(complete_stage, complete_releases),
        )
    assert set(path.name for path in complete_stage.iterdir()) == {
        "bars.parquet",
        "validation_summary.json",
    }
    monkeypatch.setattr(
        alpaca_module,
        "build_manifest",
        original_build_manifest,
    )
    recovered_manifest, recovered = materialize_qualification_release(
        result,
        staging_dir=complete_stage,
        release_root=complete_releases,
        **kwargs,
        **publication_kwargs(complete_stage, complete_releases),
    )
    assert recovered.name == recovered_manifest.release_id

    tampered_stage = tmp_path / "tampered-stage"
    tampered_releases = tmp_path / "tampered-releases"
    monkeypatch.setattr(alpaca_module, "atomic_write_new", interrupt_before_summary)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        materialize_qualification_release(
            result,
            staging_dir=tampered_stage,
            release_root=tampered_releases,
            **kwargs,
            **publication_kwargs(tampered_stage, tampered_releases),
        )
    (tampered_stage / "bars.parquet").write_bytes(b"tampered")
    monkeypatch.setattr(
        alpaca_module,
        "atomic_write_new",
        original_atomic_write_new,
    )
    with pytest.raises(IntegrityError, match="differs from the exact request"):
        materialize_qualification_release(
            result,
            staging_dir=tampered_stage,
            release_root=tampered_releases,
            **kwargs,
            **publication_kwargs(tampered_stage, tampered_releases),
        )

    unknown_stage = tmp_path / "unknown-stage"
    unknown_stage.mkdir()
    (unknown_stage / "operator-note.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(IntegrityError, match="unrecognized partial state"):
        materialize_qualification_release(
            result,
            staging_dir=unknown_stage,
            release_root=tmp_path / "unknown-releases",
            **kwargs,
            **publication_kwargs(
                unknown_stage,
                tmp_path / "unknown-releases",
            ),
        )
    assert (unknown_stage / "operator-note.txt").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("symbol", ["abc", " ABC ", "AbC"])
def test_alpaca_native_symbol_requires_exact_canonical_wire_text(
    tmp_path: Path,
    symbol: str,
) -> None:
    with pytest.raises(ContractError, match="exact canonical text"):
        reparse_native_raw(
            _native_manifest_with_symbol(
                tmp_path / symbol.replace(" ", "_"),
                symbol,
            )
        )


def test_alpaca_duplicate_and_pagination_poison_fail(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="duplicate"):
        reparse_native_raw(_native_manifest(tmp_path / "dup", duplicate=True))
    other = tmp_path / "token"
    other.mkdir()
    with pytest.raises(ContractError, match="token breaks"):
        reparse_native_raw(_native_manifest(other, broken_token=True))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_token_in", True),
        ("page_token_in", 1),
        ("page_token_in", ""),
        ("next_page_token_expected", True),
        ("next_page_token_expected", 1),
        ("next_page_token_expected", ""),
    ],
)
def test_alpaca_manifest_pagination_tokens_require_exact_nonempty_text_or_null(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    manifest = _native_manifest(tmp_path)
    request = manifest.requests[0]
    poisoned_page = replace(request.pages[0], **{field: value})
    poisoned_request = replace(
        request,
        pages=(poisoned_page, *request.pages[1:]),
    )
    with pytest.raises(ContractError, match="pagination tokens"):
        reparse_native_raw(replace(manifest, requests=(poisoned_request,)))


@pytest.mark.parametrize("value", [True, 1, "", [], {}])
def test_alpaca_raw_next_page_token_requires_exact_nonempty_text_or_null(
    tmp_path: Path,
    value: object,
) -> None:
    manifest = _native_manifest(tmp_path)
    request = manifest.requests[0]
    page = request.pages[0]
    payload = json.loads(gzip.decompress(page.path.read_bytes()))
    payload["next_page_token"] = value
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    page.path.write_bytes(gzip.compress(raw, mtime=0))
    changed_page = replace(
        page,
        sha256=sha256_file(page.path),
        uncompressed_bytes=len(raw),
    )
    changed_request = replace(
        request,
        pages=(changed_page, *request.pages[1:]),
    )
    with pytest.raises(ContractError, match="pagination tokens"):
        reparse_native_raw(replace(manifest, requests=(changed_request,)))


def test_rejected_alpaca_bar_does_not_commit_duplicate_state() -> None:
    seen: set[tuple[str, object]] = set()
    valid = {
        "t": "2026-07-13T04:00:00Z",
        "o": 10.0,
        "h": 11.0,
        "l": 9.0,
        "c": 10.5,
        "v": 100,
    }
    with pytest.raises(ContractError, match="New York midnight"):
        _accept_native_bar(
            symbol="ABC",
            bar={**valid, "t": "2026-07-13T05:00:00Z"},
            eastern=ZoneInfo("America/New_York"),
            seen_keys=seen,
        )
    assert seen == set()
    with pytest.raises(ContractError, match="OHLCV invariants"):
        _accept_native_bar(
            symbol="ABC",
            bar={**valid, "v": -1},
            eastern=ZoneInfo("America/New_York"),
            seen_keys=seen,
        )
    assert seen == set()
    accepted = _accept_native_bar(
        symbol="ABC",
        bar=valid,
        eastern=ZoneInfo("America/New_York"),
        seen_keys=seen,
    )
    assert accepted[1].isoformat() == "2026-07-13"
    assert seen == {("ABC", accepted[1])}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("v", True, "volume must be an exact JSON integer"),
        ("v", 1.5, "volume must be an exact JSON integer"),
        ("v", "1", "volume must be an exact JSON integer"),
        ("n", False, "trade count must be an exact JSON integer"),
        ("n", 1.5, "trade count must be an exact JSON integer"),
        ("n", "1", "trade count must be an exact JSON integer"),
    ],
)
def test_alpaca_integer_domain_rejects_coercible_values(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ContractError, match=message):
        reparse_native_raw(
            _native_manifest_with_integer_value(
                tmp_path / f"{field}-{type(value).__name__}",
                field=field,
                value=value,
            )
        )


@pytest.mark.parametrize(("field", "value"), [("v", 0), ("n", 0), ("n", None)])
def test_alpaca_integer_domain_accepts_exact_nonnegative_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    result = reparse_native_raw(
        _native_manifest_with_integer_value(
            tmp_path / f"valid-{field}-{value}",
            field=field,
            value=value,
        )
    )
    assert result.row_count == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("o", True, "OHLC values must be exact JSON numbers"),
        ("h", False, "OHLC values must be exact JSON numbers"),
        ("l", "9.0", "OHLC values must be exact JSON numbers"),
        ("c", None, "OHLC values must be exact JSON numbers"),
        ("vw", True, "VWAP must be an exact JSON number or null"),
        ("vw", "10.2", "VWAP must be an exact JSON number or null"),
    ],
)
def test_alpaca_price_domain_rejects_boolean_and_coercible_values(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ContractError, match=message):
        reparse_native_raw(
            _native_manifest_with_integer_value(
                tmp_path / f"price-{field}-{type(value).__name__}",
                field=field,
                value=value,
            )
        )


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-13T00:00:00-04:00",
        "2026-07-13T04:00:00+00:00",
        20260713,
    ],
)
def test_alpaca_timestamp_requires_exact_canonical_utc_z_encoding(
    tmp_path: Path,
    value: object,
) -> None:
    with pytest.raises(ContractError, match="canonical UTC Z encoding"):
        reparse_native_raw(
            _native_manifest_with_timestamp(
                tmp_path / f"timestamp-{type(value).__name__}",
                value,
            )
        )
