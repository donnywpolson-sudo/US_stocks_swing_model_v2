from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..common import parse_timestamp, reject_link, sha256_file
from ..errors import ContractError, IntegrityError
from .parquet import deterministic_table, write_deterministic_parquet


HFDL_BREAK = date(2022, 3, 4)
HFDL_HISTORICAL_AVAILABILITY = "UNKNOWN_NOT_AS_RECEIVED"
HFDL_POINT_IN_TIME_STATE = "HISTORICAL_PROXY"
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


def validate_and_tag_hfdl(parquet_path: Path, sidecar_path: Path) -> HfdlValidationResult:
    parquet_file = Path(parquet_path)
    sidecar_file = Path(sidecar_path)
    for candidate in (parquet_file, sidecar_file):
        reject_link(candidate)
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise ContractError(f"HFDL input must be an independent plain file: {candidate}")
    try:
        sidecar = json.loads(sidecar_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("HFDL sidecar is invalid JSON") from exc
    parquet_hash = sha256_file(parquet_file)
    required = {
        "canonical_symbol",
        "created_at_utc",
        "row_count",
        "sha256",
        "timeframe",
        "validation_passed",
        "version",
        "source_limitations",
    }
    if not required <= sidecar.keys():
        raise ContractError(f"HFDL sidecar lacks fields: {sorted(required-sidecar.keys())}")
    if sidecar["sha256"] != parquet_hash:
        raise IntegrityError("HFDL sidecar hash differs from Parquet")
    if sidecar["timeframe"] != "daily" or sidecar["version"] != "clean" or sidecar["validation_passed"] is not True:
        raise ContractError("HFDL payload is not a passed daily-clean legacy input")
    limitations = " ".join(str(item).lower() for item in sidecar["source_limitations"])
    if not all(token in limitations for token in ("fixed", "march 2022", "source-adjusted")):
        raise ContractError("HFDL sidecar does not preserve required source limitations")
    # This is when the legacy file/sidecar was produced or retrieved. It is not
    # an as-received observation time for any historical session.
    source_retrieved_at = parse_timestamp(sidecar["created_at_utc"], "created_at_utc")
    table = pq.read_table(parquet_file)
    if tuple(table.column_names) != HFDL_COLUMNS or table.schema.remove_metadata() != HFDL_NATIVE_SCHEMA:
        raise ContractError("HFDL Parquet schema differs from the exact native contract")
    rows = table.to_pylist()
    if len(rows) != sidecar["row_count"]:
        raise IntegrityError("HFDL row count differs from sidecar")
    symbol = str(sidecar["canonical_symbol"]).upper()
    output: list[dict[str, object]] = []
    seen: set[date] = set()
    previous: date | None = None
    epoch_counts = {"hfdl_pitrading_consolidated": 0, "hfdl_iex_only": 0}
    for row in rows:
        if str(row["ticker"]).upper() != symbol or str(row["per"]).upper() != "D":
            raise ContractError("HFDL row identity/timeframe differs from sidecar")
        try:
            session = datetime.strptime(str(row["date"]), "%Y%m%d").date()
        except ValueError as exc:
            raise ContractError("HFDL row date must use native YYYYMMDD format") from exc
        if str(row["time"]) != "000000":
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
