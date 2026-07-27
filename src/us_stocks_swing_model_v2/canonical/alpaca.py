from __future__ import annotations

import gzip
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa

from ..capabilities import SyntheticOnlyPermit, require_synthetic_permit
from ..common import (
    atomic_write_new,
    canonical_json_bytes,
    iso_z,
    parse_timestamp,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..errors import ContractError, IntegrityError
from ..releases import AtomicReleasePublisher, ReleaseManifest, build_manifest
from .parquet import (
    deterministic_parquet_bytes,
    deterministic_table,
    write_deterministic_parquet,
)


ALPACA_SCHEMA = pa.schema(
    [
        ("provider_symbol", pa.string()),
        ("asset_id", pa.string()),
        ("session", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("trade_count", pa.int64()),
        ("vwap", pa.float64()),
        ("bar_event_at", pa.timestamp("us", tz="UTC")),
        ("retrieved_at", pa.timestamp("us", tz="UTC")),
        ("source_epoch", pa.string()),
        ("evidence_class", pa.string()),
        ("quality_state", pa.string()),
        ("point_in_time_safe", pa.bool_()),
    ]
)
KNOWN_BAR_FIELDS = {"o", "h", "l", "c", "v", "n", "vw", "t"}
SYNTHETIC_QUALIFICATION_PUBLICATION_SCOPE = (
    "SYNTHETIC_ALPACA_QUALIFICATION_RELEASE_PUBLICATION"
)


@dataclass(frozen=True)
class NativePageSpec:
    path: Path
    sha256: str
    uncompressed_bytes: int
    page_index: int
    page_token_in: str | None
    next_page_token_expected: str | None


@dataclass(frozen=True)
class NativeRequestSpec:
    request_id: str
    pages: tuple[NativePageSpec, ...]


@dataclass(frozen=True)
class AlpacaNativeManifest:
    feed: str
    timeframe: str
    adjustment: str
    asof: str
    retrieved_at: datetime
    source_epoch: str
    quality_state: str
    requests: tuple[NativeRequestSpec, ...]
    symbol_to_asset_id: dict[str, str]

    def validate(self) -> None:
        if (self.feed, self.timeframe, self.adjustment) != ("sip", "1Day", "raw"):
            raise ContractError("legacy Alpaca canonicalization accepts only SIP 1Day raw pages")
        if self.asof not in {"-", "legacy_default_mapping"}:
            raise ContractError("Alpaca asof semantics must be explicit")
        parse_timestamp(iso_z(self.retrieved_at), "retrieved_at")
        if not self.source_epoch or self.quality_state != "FAIL":
            raise ContractError("legacy Alpaca source epoch and failed quality state are binding")
        request_ids = [request.request_id for request in self.requests]
        if not request_ids or request_ids != list(dict.fromkeys(request_ids)):
            raise ContractError("native request IDs must be nonempty and unique")
        for request in self.requests:
            for page in request.pages:
                for token in (
                    page.page_token_in,
                    page.next_page_token_expected,
                ):
                    if token is not None and (
                        type(token) is not str or not token
                    ):
                        raise ContractError(
                            "native Alpaca pagination tokens must be exact "
                            "nonempty strings or null"
                        )


@dataclass(frozen=True)
class AlpacaCanonicalResult:
    table: pa.Table
    page_hashes: tuple[str, ...]
    row_count: int
    unresolved_identity_rows: int
    source_epoch: str
    quality_state: str = "FAIL"
    role: str = "qualification_evidence_only"


def _accept_native_bar(
    *,
    symbol: str,
    bar: object,
    eastern: ZoneInfo,
    seen_keys: set[tuple[str, object]],
) -> tuple[datetime, object, float, float, float, float, int, int | None, float | None]:
    if (
        not isinstance(bar, dict)
        or not {"o", "h", "l", "c", "v", "t"} <= set(bar)
        or set(bar) - KNOWN_BAR_FIELDS
    ):
        raise ContractError(
            "native Alpaca bar schema differs from the frozen contract"
        )
    event_at = parse_utc_z(bar["t"], "bar.t")
    local_event = event_at.astimezone(eastern)
    if any(
        (
            local_event.hour,
            local_event.minute,
            local_event.second,
            local_event.microsecond,
        )
    ):
        raise ContractError(
            "native Alpaca daily timestamp must be New York midnight"
        )
    session = local_event.date()
    if any(
        isinstance(bar[name], bool)
        or not isinstance(bar[name], (int, float))
        for name in ("o", "h", "l", "c")
    ):
        raise ContractError(
            "native Alpaca OHLC values must be exact JSON numbers"
        )
    if bar.get("vw") is not None and (
        isinstance(bar["vw"], bool)
        or not isinstance(bar["vw"], (int, float))
    ):
        raise ContractError(
            "native Alpaca VWAP must be an exact JSON number or null"
        )
    open_, high, low, close = (
        float(bar[name])
        for name in ("o", "h", "l", "c")
    )
    if type(bar["v"]) is not int:
        raise ContractError(
            "native Alpaca volume must be an exact JSON integer"
        )
    if bar.get("n") is not None and type(bar["n"]) is not int:
        raise ContractError(
            "native Alpaca trade count must be an exact JSON integer or null"
        )
    volume = bar["v"]
    trade_count = bar.get("n")
    vwap = None if bar.get("vw") is None else float(bar["vw"])
    if (
        not all(
            math.isfinite(value) and value > 0
            for value in (open_, high, low, close)
        )
        or high < max(open_, close)
        or low > min(open_, close)
        or high < low
        or volume < 0
        or trade_count is not None and trade_count < 0
        or vwap is not None and not math.isfinite(vwap)
    ):
        raise ContractError("native Alpaca bar violates OHLCV invariants")
    key = (symbol, session)
    if key in seen_keys:
        raise ContractError(
            f"duplicate Alpaca symbol/session: {symbol}/{session}"
        )
    seen_keys.add(key)
    return (
        event_at,
        session,
        open_,
        high,
        low,
        close,
        volume,
        trade_count,
        vwap,
    )


def reparse_native_raw(manifest: AlpacaNativeManifest) -> AlpacaCanonicalResult:
    manifest.validate()
    records: list[dict[str, object]] = []
    seen_keys: set[tuple[str, object]] = set()
    page_hashes: list[str] = []
    unresolved = 0
    eastern = ZoneInfo("America/New_York")
    for request in manifest.requests:
        if [page.page_index for page in request.pages] != list(range(1, len(request.pages) + 1)):
            raise ContractError(f"page indices are not contiguous: {request.request_id}")
        previous_next: str | None = None
        for page in request.pages:
            if page.page_token_in != previous_next:
                raise ContractError(f"pagination input token breaks chain: {request.request_id}/{page.page_index}")
            reject_link(page.path)
            if not page.path.is_file() or page.path.stat().st_nlink != 1 or sha256_file(page.path) != page.sha256:
                raise IntegrityError(f"native page hash/plain-file check failed: {page.path}")
            compressed = page.path.read_bytes()
            try:
                raw = gzip.decompress(compressed)
            except gzip.BadGzipFile as exc:
                raise ContractError(f"native page is not gzip: {page.path}") from exc
            if len(raw) != page.uncompressed_bytes:
                raise IntegrityError(f"native page uncompressed byte count differs: {page.path}")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ContractError(f"native page JSON is invalid: {page.path}") from exc
            if not isinstance(payload, dict) or set(payload) != {"bars", "next_page_token"} or not isinstance(payload["bars"], dict):
                raise ContractError("native Alpaca page schema differs from bars/next_page_token")
            next_token = payload["next_page_token"]
            if next_token is not None and (
                type(next_token) is not str or not next_token
            ):
                raise ContractError(
                    "native Alpaca pagination tokens must be exact nonempty "
                    "strings or null"
                )
            if next_token != page.next_page_token_expected:
                raise IntegrityError("native pagination token differs from evidence manifest")
            previous_next = next_token
            page_hashes.append(page.sha256)
            for raw_symbol, bars in payload["bars"].items():
                if (
                    type(raw_symbol) is not str
                    or not raw_symbol
                    or raw_symbol != raw_symbol.strip().upper()
                ):
                    raise ContractError(
                        "native Alpaca symbol must be exact canonical text"
                    )
                symbol = raw_symbol
                if not isinstance(bars, list):
                    raise ContractError("native Alpaca symbol/bars shape is invalid")
                for bar in bars:
                    (
                        event_at,
                        session,
                        open_,
                        high,
                        low,
                        close,
                        volume,
                        trade_count,
                        vwap,
                    ) = _accept_native_bar(
                        symbol=symbol,
                        bar=bar,
                        eastern=eastern,
                        seen_keys=seen_keys,
                    )
                    asset_id = manifest.symbol_to_asset_id.get(symbol)
                    if asset_id is None:
                        unresolved += 1
                    records.append(
                        {
                            "provider_symbol": symbol,
                            "asset_id": asset_id,
                            "session": session,
                            "open": open_,
                            "high": high,
                            "low": low,
                            "close": close,
                            "volume": volume,
                            "trade_count": trade_count,
                            "vwap": vwap,
                            "bar_event_at": event_at,
                            "retrieved_at": manifest.retrieved_at,
                            "source_epoch": manifest.source_epoch,
                            "evidence_class": "QUALIFICATION_EVIDENCE",
                            "quality_state": "FAIL",
                            "point_in_time_safe": False,
                        }
                    )
        if previous_next is not None:
            raise ContractError(f"native request pagination is incomplete: {request.request_id}")
    table = deterministic_table(pa.Table.from_pylist(records, schema=ALPACA_SCHEMA), ALPACA_SCHEMA, ("provider_symbol", "session"))
    return AlpacaCanonicalResult(
        table=table,
        page_hashes=tuple(page_hashes),
        row_count=table.num_rows,
        unresolved_identity_rows=unresolved,
        source_epoch=manifest.source_epoch,
    )


def qualification_publication_binding_id(
    result: AlpacaCanonicalResult,
    *,
    staging_dir: Path,
    release_root: Path,
    created_at: str,
    code_hash: str,
    config_hash: str,
    environment_hash: str,
) -> str:
    stage = Path(staging_dir)
    releases = Path(release_root)
    if not stage.is_absolute() or not releases.is_absolute():
        raise ContractError(
            "Alpaca qualification staging and release roots must be absolute"
        )
    parse_utc_z(created_at, "alpaca_qualification.created_at")
    for name, value in (
        ("code_hash", code_hash),
        ("config_hash", config_hash),
        ("environment_hash", environment_hash),
    ):
        require_sha256(value, f"alpaca_qualification.{name}")
    if (
        result.table.schema != ALPACA_SCHEMA
        or type(result.row_count) is not int
        or result.row_count != result.table.num_rows
        or type(result.unresolved_identity_rows) is not int
        or not 0 <= result.unresolved_identity_rows <= result.row_count
        or type(result.page_hashes) is not tuple
        or not result.page_hashes
        or result.quality_state != "FAIL"
        or result.role != "qualification_evidence_only"
        or type(result.source_epoch) is not str
        or not result.source_epoch
    ):
        raise ContractError(
            "Alpaca qualification result differs from the exact failed-evidence contract"
        )
    for index, page_hash in enumerate(result.page_hashes):
        require_sha256(page_hash, f"alpaca_qualification.page_hashes[{index}]")
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, result.table.schema) as writer:
        writer.write_table(result.table)
    payload = {
        "schema_version": 1,
        "scope": SYNTHETIC_QUALIFICATION_PUBLICATION_SCOPE,
        "staging_dir": str(stage),
        "release_root": str(releases),
        "created_at": created_at,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "environment_hash": environment_hash,
        "source_epoch": result.source_epoch,
        "row_count": result.row_count,
        "unresolved_identity_rows": result.unresolved_identity_rows,
        "page_hashes": list(result.page_hashes),
        "table_ipc_sha256": sha256_bytes(sink.getvalue().to_pybytes()),
    }
    return sha256_bytes(canonical_json_bytes(payload))


def materialize_qualification_release(
    result: AlpacaCanonicalResult,
    *,
    staging_dir: Path,
    release_root: Path,
    created_at: str,
    code_hash: str,
    config_hash: str,
    environment_hash: str,
    publication_synthetic_permit: SyntheticOnlyPermit | None = None,
    publication_allowed_root: Path | None = None,
) -> tuple[ReleaseManifest, Path]:
    stage = Path(staging_dir)
    releases = Path(release_root)
    binding_id = qualification_publication_binding_id(
        result,
        staging_dir=stage,
        release_root=releases,
        created_at=created_at,
        code_hash=code_hash,
        config_hash=config_hash,
        environment_hash=environment_hash,
    )
    permit = require_synthetic_permit(
        publication_synthetic_permit,
        scope=SYNTHETIC_QUALIFICATION_PUBLICATION_SCOPE,
    )
    if permit.fixture_id != binding_id:
        raise ContractError(
            "Alpaca qualification publication permit differs from the exact request"
        )
    if publication_allowed_root is None:
        raise ContractError(
            "Alpaca qualification publication requires an explicit allowed root"
        )
    allowed_root = Path(publication_allowed_root)
    require_contained_path(allowed_root, allowed_root)
    require_contained_path(stage, allowed_root, must_exist=stage.exists())
    require_contained_path(releases, allowed_root, must_exist=releases.exists())
    summary = {
        "schema_version": 1,
        "role": result.role,
        "quality_state": result.quality_state,
        "source_epoch": result.source_epoch,
        "row_count": result.row_count,
        "unresolved_identity_rows": result.unresolved_identity_rows,
        "page_hashes": list(result.page_hashes),
        "active_eligible": False,
    }
    bars_path = stage / "bars.parquet"
    summary_path = stage / "validation_summary.json"
    expected_bars = deterministic_parquet_bytes(
        result.table,
        schema=ALPACA_SCHEMA,
        sort_keys=("provider_symbol", "session"),
    )
    expected_summary = canonical_json_bytes(summary)
    stage.mkdir(parents=True, exist_ok=True)
    reject_link(stage)
    entries = {entry.name: entry for entry in stage.iterdir()}
    unexpected = set(entries) - {"bars.parquet", "validation_summary.json"}
    if unexpected or ("validation_summary.json" in entries and "bars.parquet" not in entries):
        raise IntegrityError(
            "Alpaca canonical staging directory contains an unrecognized partial state"
        )
    for name, expected in (
        ("bars.parquet", expected_bars),
        ("validation_summary.json", expected_summary),
    ):
        path = stage / name
        if path.exists():
            reject_link(path)
            if (
                not path.is_file()
                or path.stat().st_nlink != 1
                or path.read_bytes() != expected
            ):
                raise IntegrityError(
                    f"Alpaca canonical staged {name} differs from the exact request"
                )
            continue
        if name == "bars.parquet":
            write_deterministic_parquet(
                result.table,
                path,
                schema=ALPACA_SCHEMA,
                sort_keys=("provider_symbol", "session"),
            )
        else:
            atomic_write_new(path, expected)
    sessions = result.table.column("session").to_pylist()
    manifest = build_manifest(
        stage,
        ["bars.parquet", "validation_summary.json"],
        project="US_stocks_swing_model_v2",
        dataset="alpaca_legacy_qualification_bars",
        source_epoch=result.source_epoch,
        role="qualification_evidence_only",
        quality_state="FAIL",
        created_at=created_at,
        row_count=result.row_count,
        event_start=str(min(sessions)) if sessions else None,
        event_end=str(max(sessions)) if sessions else None,
        schema_fingerprint=sha256_bytes(str(ALPACA_SCHEMA).encode()),
        code_hash=code_hash,
        config_hash=config_hash,
        environment_hash=environment_hash,
    )
    published = AtomicReleasePublisher(releases).publish(stage, manifest)
    return manifest, published
