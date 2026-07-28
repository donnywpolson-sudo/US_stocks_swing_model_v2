from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..common import parse_utc_z, reject_link, require_sha256, sha256_file
from ..errors import ContractError, IntegrityError
from .parquet import deterministic_table, write_deterministic_parquet


HFDL_BREAK = date(2022, 3, 4)
HFDL_HISTORICAL_AVAILABILITY = "UNKNOWN_NOT_AS_RECEIVED"
HFDL_POINT_IN_TIME_STATE = "HISTORICAL_PROXY"
HFDL_SIDECAR_EXTENSION_POLICY = "ALLOW_UNTRUSTED_IGNORED_EXTENSION_FIELDS"
HFDL_SIDECAR_REQUIRED_FIELDS = (
    "canonical_symbol",
    "created_at_utc",
    "row_count",
    "sha256",
    "timeframe",
    "validation_passed",
    "version",
    "source_limitations",
)
HFDL_COLUMNS = ("ticker", "per", "date", "time", "open", "high", "low", "close", "vol", "openint")
HFDL_NATIVE_SCHEMA = pa.schema(
    [
        # The approved 1,388-file legacy corpus was written by pandas with
        # Arrow's exact ``large_string`` physical type.  Pin that real source
        # contract rather than accepting an arbitrary string-like schema.
        ("ticker", pa.large_string()),
        ("per", pa.large_string()),
        ("date", pa.large_string()),
        ("time", pa.large_string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("vol", pa.int64()),
        ("openint", pa.int64()),
    ]
)
HFDL_TAGGED_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("session", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("source_epoch", pa.string()),
        ("source_adjustment", pa.string()),
        ("evidence_class", pa.string()),
        ("point_in_time_safe", pa.bool_()),
        ("point_in_time_state", pa.string()),
        ("historical_availability_state", pa.string()),
        ("source_retrieved_at", pa.timestamp("us", tz="UTC")),
    ]
)


@dataclass(frozen=True)
class HfdlValidationResult:
    table: pa.Table
    symbol: str
    parquet_sha256: str
    sidecar_sha256: str
    row_count: int
    epoch_counts: dict[str, int]
    quality_state: str = "LEGACY_DISCOVERY_VALIDATED_WITH_SOURCE_LIMITATIONS"
    historical_availability_state: str = HFDL_HISTORICAL_AVAILABILITY
    point_in_time_state: str = HFDL_POINT_IN_TIME_STATE

    def release_metadata(self) -> dict[str, str]:
        """Mandatory provenance for any release built from this legacy input."""
        return {
            "quality_state": self.quality_state,
            "evidence_class": "LEGACY_DISCOVERY",
            "historical_availability_state": self.historical_availability_state,
            "point_in_time_state": self.point_in_time_state,
        }


def _parse_hfdl_created_at(
    value: str,
    *,
    allow_migrated_utc_offset: bool,
) -> datetime:
    try:
        return parse_utc_z(value, "created_at_utc")
    except ContractError:
        if allow_migrated_utc_offset and value.endswith("+00:00"):
            return parse_utc_z(value[:-6] + "Z", "created_at_utc")
        raise


def load_validated_hfdl_sidecar(
    sidecar_path: Path,
    *,
    _allow_migrated_utc_offset: bool = False,
) -> dict[str, object]:
    """Load the one canonical HFDL sidecar contract.

    Undeclared fields are retained only as untrusted compatibility input: they
    are ignored and never returned, propagated, or treated as evidence.
    """

    sidecar_file = Path(sidecar_path)
    reject_link(sidecar_file)
    if not sidecar_file.is_file() or sidecar_file.stat().st_nlink != 1:
        raise ContractError(
            f"HFDL input must be an independent plain file: {sidecar_file}"
        )
    try:
        raw = json.loads(sidecar_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("HFDL sidecar is invalid JSON") from exc
    required = set(HFDL_SIDECAR_REQUIRED_FIELDS)
    if not isinstance(raw, dict) or not required <= set(raw):
        raise ContractError("HFDL sidecar lacks required evidence fields")
    sidecar = {field: raw[field] for field in HFDL_SIDECAR_REQUIRED_FIELDS}
    if (
        type(sidecar["canonical_symbol"]) is not str
        or not sidecar["canonical_symbol"].strip()
        or type(sidecar["created_at_utc"]) is not str
        or type(sidecar["row_count"]) is not int
        or sidecar["row_count"] < 1
        or type(sidecar["sha256"]) is not str
        or type(sidecar["timeframe"]) is not str
        or type(sidecar["validation_passed"]) is not bool
        or type(sidecar["version"]) is not str
        or type(sidecar["source_limitations"]) is not list
        or not sidecar["source_limitations"]
        or any(
            type(item) is not str or not item
            for item in sidecar["source_limitations"]
        )
    ):
        raise ContractError("HFDL sidecar evidence types are invalid")
    require_sha256(sidecar["sha256"], "hfdl_sidecar.sha256")
    if (
        sidecar["timeframe"] != "daily"
        or sidecar["version"] != "clean"
        or sidecar["validation_passed"] is not True
    ):
        raise ContractError("HFDL payload is not a passed daily-clean legacy input")
    limitations = " ".join(
        item.lower() for item in sidecar["source_limitations"]
    )
    if not all(
        token in limitations for token in ("fixed", "march 2022", "source-adjusted")
    ):
        raise ContractError(
            "HFDL sidecar does not preserve required source limitations"
        )
    _parse_hfdl_created_at(
        sidecar["created_at_utc"],
        allow_migrated_utc_offset=_allow_migrated_utc_offset,
    )
    return sidecar


def validate_and_tag_hfdl(
    parquet_path: Path,
    sidecar_path: Path,
    *,
    _allow_migrated_utc_offset: bool = False,
) -> HfdlValidationResult:
    parquet_file = Path(parquet_path)
    sidecar_file = Path(sidecar_path)
    for candidate in (parquet_file, sidecar_file):
        reject_link(candidate)
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise ContractError(f"HFDL input must be an independent plain file: {candidate}")
    sidecar = load_validated_hfdl_sidecar(
        sidecar_file,
        _allow_migrated_utc_offset=_allow_migrated_utc_offset,
    )
    parquet_hash = sha256_file(parquet_file)
    if sidecar["sha256"] != parquet_hash:
        raise IntegrityError("HFDL sidecar hash differs from Parquet")
    # This is when the legacy file/sidecar was produced or retrieved. It is not
    # an as-received observation time for any historical session.
    source_retrieved_at = _parse_hfdl_created_at(
        sidecar["created_at_utc"],
        allow_migrated_utc_offset=_allow_migrated_utc_offset,
    )
    table = pq.read_table(parquet_file)
    if tuple(table.column_names) != HFDL_COLUMNS or table.schema.remove_metadata() != HFDL_NATIVE_SCHEMA:
        raise ContractError("HFDL Parquet schema differs from the exact native contract")
    rows = table.to_pylist()
    if len(rows) != sidecar["row_count"]:
        raise IntegrityError("HFDL row count differs from sidecar")
    raw_symbol = sidecar["canonical_symbol"]
    if raw_symbol != raw_symbol.strip().upper():
        raise ContractError(
            "HFDL canonical symbol must be exact uppercase wire text"
        )
    symbol = raw_symbol
    output: list[dict[str, object]] = []
    seen: set[date] = set()
    previous: date | None = None
    epoch_counts = {"hfdl_pitrading_consolidated": 0, "hfdl_iex_only": 0}
    for row in rows:
        if (
            type(row["ticker"]) is not str
            or row["ticker"] != symbol
            or type(row["per"]) is not str
            or row["per"] != "D"
            or type(row["date"]) is not str
            or type(row["time"]) is not str
        ):
            raise ContractError("HFDL row identity/timeframe differs from sidecar")
        try:
            session = datetime.strptime(row["date"], "%Y%m%d").date()
        except ValueError as exc:
            raise ContractError("HFDL row date must use native YYYYMMDD format") from exc
        if row["time"] != "000000":
            raise ContractError("daily HFDL row time must use native 000000 format")
        if session in seen or previous is not None and session <= previous:
            raise ContractError("HFDL sessions must be unique and strictly increasing")
        seen.add(session)
        previous = session
        open_, high, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
        volume = int(row["vol"])
        if (
            not all(math.isfinite(value) and value > 0 for value in (open_, high, low, close))
            or high < max(open_, close)
            or low > min(open_, close)
            or high < low
            or volume < 0
        ):
            raise ContractError("HFDL row violates OHLCV invariants")
        epoch = "hfdl_pitrading_consolidated" if session < HFDL_BREAK else "hfdl_iex_only"
        epoch_counts[epoch] += 1
        output.append(
            {
                "symbol": symbol,
                "session": session,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "source_epoch": epoch,
                "source_adjustment": "hfdl_clean_source_adjusted",
                "evidence_class": "LEGACY_DISCOVERY",
                "point_in_time_safe": False,
                "point_in_time_state": HFDL_POINT_IN_TIME_STATE,
                "historical_availability_state": HFDL_HISTORICAL_AVAILABILITY,
                "source_retrieved_at": source_retrieved_at,
            }
        )
    tagged = deterministic_table(pa.Table.from_pylist(output, schema=HFDL_TAGGED_SCHEMA), HFDL_TAGGED_SCHEMA, ("symbol", "session"))
    return HfdlValidationResult(
        table=tagged,
        symbol=symbol,
        parquet_sha256=parquet_hash,
        sidecar_sha256=sha256_file(sidecar_file),
        row_count=len(rows),
        epoch_counts=epoch_counts,
    )


def write_tagged_hfdl_legacy(result: HfdlValidationResult, output_path: Path) -> Path:
    epochs = set(result.table.column("source_epoch").to_pylist())
    if len(epochs) != 1:
        raise ContractError("HFDL source epochs must be published as separate releases")
    return write_deterministic_parquet(
        result.table, output_path, schema=HFDL_TAGGED_SCHEMA, sort_keys=("symbol", "session")
    )


def write_tagged_hfdl_legacy_epochs(
    result: HfdlValidationResult,
    output_root: Path,
) -> dict[str, Path]:
    root = Path(output_root)
    outputs: dict[str, Path] = {}
    for epoch in sorted(result.epoch_counts):
        if result.epoch_counts[epoch] == 0:
            continue
        mask = pc.equal(result.table.column("source_epoch"), pa.scalar(epoch))
        table = result.table.filter(mask)
        path = root / epoch / f"{result.symbol}.parquet"
        outputs[epoch] = write_deterministic_parquet(
            table,
            path,
            schema=HFDL_TAGGED_SCHEMA,
            sort_keys=("symbol", "session"),
        )
    if not outputs:
        raise ContractError("validated HFDL input contains no publishable epoch")
    return outputs
