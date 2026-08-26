from __future__ import annotations

import gzip
import hashlib
import json
import math
import ntpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import (
    canonical_json_bytes,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from .errors import ContractError, IntegrityError
from .releases import verify_accepted_release


POLICY_PATH = Path("config/alpaca_archive_rehabilitation_policy.json")
RETIRED_MODE = "ALPACA_LEGACY_ARCHIVE_REHABILITATION_RETIRED_ACCEPTED_RELEASE_ONLY"
BAR_KEYS = {"c", "h", "l", "n", "o", "t", "v", "vw"}


@dataclass(frozen=True)
class ArchiveExpectations:
    source: str
    source_route_name: str
    feed: str
    timeframe: str
    adjustment: str
    request_start_date: str
    request_end_date: str
    symbol_count: int
    page_count: int
    chunk_count: int
    row_count: int
    event_start: str
    event_end: str
    compressed_bytes: int
    uncompressed_bytes: int


def _strict_mapping(
    value: object, expected_keys: set[str], field: str
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected_keys:
        raise ContractError(f"{field} schema differs")
    return value


def _exact_windows_path(value: Path | str) -> str:
    return ntpath.normcase(ntpath.normpath(str(value).replace("/", "\\")))


def _require_regular_file(path: Path, allowed_root: Path) -> Path:
    require_contained_path(path, allowed_root)
    reject_link(path)
    if not path.is_file():
        raise ContractError(f"required archive input is not a file: {path}")
    if path.stat().st_nlink != 1:
        raise ContractError(f"hardlinked archive input is prohibited: {path}")
    return path


def _read_json_file(path: Path, allowed_root: Path, field: str) -> Any:
    _require_regular_file(path, allowed_root)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{field} is unreadable") from exc


def _is_number(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value))


def _validate_bar(
    value: object,
    *,
    symbol: str,
    last_timestamp: dict[str, str],
) -> str:
    bar = _strict_mapping(value, BAR_KEYS, f"bar[{symbol}]")
    timestamp = bar["t"]
    if type(timestamp) is not str or len(timestamp) < 10:
        raise ContractError("bar timestamp is invalid")
    previous = last_timestamp.get(symbol)
    if previous is not None and timestamp <= previous:
        raise ContractError("duplicate or unsorted symbol timestamp")
    last_timestamp[symbol] = timestamp
    for field in ("o", "h", "l", "c", "v", "n"):
        if not _is_number(bar[field]):
            raise ContractError(f"bar field is not finite numeric: {field}")
    if bar["vw"] is not None and not _is_number(bar["vw"]):
        raise ContractError("bar field is not finite numeric: vw")
    if (
        float(bar["h"]) < max(float(bar["o"]), float(bar["c"]), float(bar["l"]))
        or float(bar["l"]) > min(float(bar["o"]), float(bar["c"]), float(bar["h"]))
        or float(bar["v"]) < 0
        or float(bar["n"]) < 0
    ):
        raise ContractError("bar violates OHLCV bounds")
    return timestamp[:10]


def _load_page(
    path: Path,
    *,
    archive_root: Path,
    expected_sha256: str,
    expected_uncompressed_bytes: int,
) -> tuple[dict[str, Any], bytes]:
    _require_regular_file(path, archive_root)
    try:
        with gzip.open(path, "rb") as stream:
            content = stream.read()
    except (OSError, EOFError) as exc:
        raise ContractError(f"native page is not valid gzip: {path}") from exc
    if (
        len(content) != expected_uncompressed_bytes
        or hashlib.sha256(content).hexdigest() != expected_sha256
    ):
        raise IntegrityError("native page decompressed identity differs")
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("native page JSON is unreadable") from exc
    return _strict_mapping(payload, {"bars", "next_page_token"}, "native page"), content


def inspect_alpaca_archive(
    archive_root: Path,
    *,
    expectations: ArchiveExpectations,
    metadata_evidence_files: Iterable[str] = (),
) -> dict[str, Any]:
    root = Path(archive_root)
    if not root.is_absolute() or not root.exists() or not root.is_dir():
        raise ContractError("archive root must be one exact existing absolute directory")
    reject_link(root)
    provenance_root = root / "bars" / "raw"
    page_root = root / "native" / "bars" / "raw"
    require_contained_path(provenance_root, root)
    require_contained_path(page_root, root)
    provenance_files = sorted(
        provenance_root.glob("*.parquet.provenance.json"),
        key=lambda value: value.name,
    )
    if len(provenance_files) != expectations.symbol_count:
        raise ContractError("provenance symbol census differs")

    symbols: set[str] = set()
    provenance_rows = 0
    min_date: str | None = None
    max_date: str | None = None
    page_bindings: dict[str, tuple[str, int]] = {}
    provenance_census: list[dict[str, object]] = []
    for path in provenance_files:
        payload = _read_json_file(path, root, "bar provenance")
        required = {
            "adjustment",
            "canonical_symbol",
            "feed",
            "max_date",
            "min_date",
            "native_pages",
            "provider_symbol",
            "request_end_date",
            "request_start_date",
            "row_count",
            "source",
            "source_route_name",
            "timeframe",
            "validation_passed",
        }
        if type(payload) is not dict or not required <= set(payload):
            raise ContractError("bar provenance schema differs")
        symbol = payload["canonical_symbol"]
        if (
            type(symbol) is not str
            or not symbol
            or payload["provider_symbol"] != symbol
            or symbol in symbols
        ):
            raise ContractError("bar provenance symbol identity differs")
        symbols.add(symbol)
        contract = (
            payload["source"],
            payload["source_route_name"],
            payload["feed"],
            payload["timeframe"],
            payload["adjustment"],
            payload["request_start_date"],
            payload["request_end_date"],
        )
        expected_contract = (
            expectations.source,
            expectations.source_route_name,
            expectations.feed,
            expectations.timeframe,
            expectations.adjustment,
            expectations.request_start_date,
            expectations.request_end_date,
        )
        if contract != expected_contract or payload["validation_passed"] is not False:
            raise ContractError("bar provenance request contract differs")
        row_count = payload["row_count"]
        if type(row_count) is not int or row_count <= 0:
            raise ContractError("bar provenance row count is invalid")
        provenance_rows += row_count
        item_min = payload["min_date"]
        item_max = payload["max_date"]
        if (
            type(item_min) is not str
            or type(item_max) is not str
            or len(item_min) != 8
            or len(item_max) != 8
        ):
            raise ContractError("bar provenance event bounds are invalid")
        min_date = item_min if min_date is None else min(min_date, item_min)
        max_date = item_max if max_date is None else max(max_date, item_max)
        native_pages = payload["native_pages"]
        if type(native_pages) is not list or not native_pages:
            raise ContractError("bar provenance native-page binding is absent")
        for index, entry_value in enumerate(native_pages):
            entry = _strict_mapping(
                entry_value,
                {"path", "sha256", "uncompressed_bytes"},
                f"native_pages[{index}]",
            )
            page_path = Path(entry["path"])
            _require_regular_file(page_path, root)
            require_contained_path(page_path, page_root)
            page_relative = page_path.relative_to(root).as_posix()
            require_sha256(entry["sha256"], "native_page.sha256")
            if (
                type(entry["uncompressed_bytes"]) is not int
                or entry["uncompressed_bytes"] <= 0
            ):
                raise ContractError("native page byte count is invalid")
            binding = (entry["sha256"], entry["uncompressed_bytes"])
            prior = page_bindings.setdefault(page_relative, binding)
            if prior != binding:
                raise IntegrityError("native page has conflicting provenance")
        provenance_census.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "symbol": symbol,
                "row_count": row_count,
            }
        )

    actual_page_paths = sorted(
        (
            path.relative_to(root).as_posix()
            for path in page_root.glob("chunk_*/page_*.json.gz")
        )
    )
    if actual_page_paths != sorted(page_bindings):
        raise ContractError("native page census differs from provenance")
    if len(actual_page_paths) != expectations.page_count:
        raise ContractError("native page count differs")

    compressed_bytes = 0
    uncompressed_bytes = 0
    native_rows = 0
    returned_symbols: set[str] = set()
    last_timestamp: dict[str, str] = {}
    event_start: str | None = None
    event_end: str | None = None
    page_census: list[dict[str, object]] = []
    chunk_pages: dict[str, list[tuple[str, bool]]] = {}
    for relative in actual_page_paths:
        path = root / Path(relative)
        expected_hash, expected_bytes = page_bindings[relative]
        payload, content = _load_page(
            path,
            archive_root=root,
            expected_sha256=expected_hash,
            expected_uncompressed_bytes=expected_bytes,
        )
        bars = payload["bars"]
        if type(bars) is not dict or not bars:
            raise ContractError("native page bars object is absent")
        for symbol, rows in bars.items():
            if type(symbol) is not str or symbol not in symbols:
                raise ContractError("native page returned an undeclared symbol")
            if type(rows) is not list or not rows:
                raise ContractError("native page symbol rows are absent")
            returned_symbols.add(symbol)
            for bar in rows:
                session = _validate_bar(
                    bar,
                    symbol=symbol,
                    last_timestamp=last_timestamp,
                )
                event_start = session if event_start is None else min(event_start, session)
                event_end = session if event_end is None else max(event_end, session)
                native_rows += 1
        token = payload["next_page_token"]
        if token is not None and (type(token) is not str or not token):
            raise ContractError("native page continuation token is malformed")
        compressed_size = path.stat().st_size
        compressed_bytes += compressed_size
        uncompressed_bytes += len(content)
        chunk = path.parent.name
        chunk_pages.setdefault(chunk, []).append((path.name, token is None))
        page_census.append(
            {
                "path": relative,
                "compressed_size": compressed_size,
                "compressed_sha256": sha256_file(path),
                "uncompressed_size": len(content),
                "uncompressed_sha256": expected_hash,
            }
        )

    for chunk, pages in chunk_pages.items():
        ordered = sorted(pages)
        expected_names = [
            f"page_{index:05d}.json.gz" for index in range(1, len(ordered) + 1)
        ]
        if [name for name, _terminal in ordered] != expected_names:
            raise ContractError(f"native page sequence differs in {chunk}")
        terminals = [terminal for _name, terminal in ordered]
        if terminals != [False] * (len(terminals) - 1) + [True]:
            raise ContractError(f"native pagination does not terminate exactly once in {chunk}")

    expected_min = expectations.event_start.replace("-", "")
    expected_max = expectations.event_end.replace("-", "")
    if (
        provenance_rows != expectations.row_count
        or native_rows != expectations.row_count
        or returned_symbols != symbols
        or len(chunk_pages) != expectations.chunk_count
        or min_date != expected_min
        or max_date != expected_max
        or event_start != expectations.event_start
        or event_end != expectations.event_end
        or compressed_bytes != expectations.compressed_bytes
        or uncompressed_bytes != expectations.uncompressed_bytes
    ):
        raise ContractError("archive content census differs from the approved expectations")

    evidence_census: list[dict[str, object]] = []
    for relative in sorted(metadata_evidence_files):
        path = root / Path(relative)
        _require_regular_file(path, root)
        evidence_census.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {
        "archive_root": str(root),
        "symbols": len(symbols),
        "pages": len(page_census),
        "chunks": len(chunk_pages),
        "rows": native_rows,
        "event_start": event_start,
        "event_end": event_end,
        "compressed_bytes": compressed_bytes,
        "uncompressed_bytes": uncompressed_bytes,
        "provenance_census_sha256": sha256_bytes(
            canonical_json_bytes(provenance_census)
        ),
        "page_census_sha256": sha256_bytes(canonical_json_bytes(page_census)),
        "metadata_evidence_census_sha256": sha256_bytes(
            canonical_json_bytes(evidence_census)
        ),
    }


def load_alpaca_archive_rehabilitation_policy(
    repository_root: Path,
) -> tuple[dict[str, Any], str]:
    root = Path(repository_root).resolve(strict=True)
    path = root / POLICY_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("Alpaca archive rehabilitation policy is unreadable") from exc
    required = {
        "schema_version",
        "policy_version",
        "project",
        "mode",
        "accepted_release",
        "input_contract",
        "evidence_boundary",
        "prospective_release",
        "legacy_universe_boundary",
        "authorities",
        "stop_conditions",
    }
    policy = _strict_mapping(payload, required, "rehabilitation policy")
    if (
        policy["schema_version"] != 2
        or policy["policy_version"] != "2.0.0"
        or policy["project"] != "US_stocks_swing_model_v2"
        or policy["mode"] != RETIRED_MODE
    ):
        raise ContractError("rehabilitation policy identity differs")
    authorities = policy["authorities"]
    if (
        type(authorities) is not dict
        or not authorities
        or any(value is not False for value in authorities.values())
    ):
        raise ContractError("rehabilitation policy grants authority")
    boundary = policy["legacy_universe_boundary"]
    if (
        type(boundary) is not dict
        or boundary != {
            "selection_state": "legacy_universe_selection_unresolved",
            "trusted_membership_claim": False,
            "active_source_eligible": False,
            "training_or_evaluation_eligible": False,
        }
    ):
        raise ContractError("rehabilitation policy weakens the legacy-universe boundary")
    accepted = policy["accepted_release"]
    if (
        type(accepted) is not dict
        or set(accepted)
        != {
            "accepted_root",
            "relative_directory",
            "release_id",
            "required_file_count",
            "required_payload_bytes",
        }
        or accepted["accepted_root"] != "data/vault/accepted"
        or accepted["relative_directory"]
        != (
            "alpaca_legacy_daily_bars/"
            "20f0fe6c054db312d83ce479c7bd14ea83be501bc19c17dfc83af830ba68c2e1"
        )
        or accepted["release_id"]
        != "20f0fe6c054db312d83ce479c7bd14ea83be501bc19c17dfc83af830ba68c2e1"
        or accepted["required_file_count"] != 201
        or accepted["required_payload_bytes"] != 99_868_172
    ):
        raise ContractError("rehabilitation accepted-release binding differs")
    return policy, sha256_bytes(canonical_json_bytes(policy))


def verify_rehabilitated_alpaca_release(
    repository_root: Path,
) -> dict[str, Any]:
    root = Path(repository_root).resolve(strict=True)
    policy, policy_id = load_alpaca_archive_rehabilitation_policy(root)
    binding = policy["accepted_release"]
    accepted_root = require_contained_path(
        root / binding["accepted_root"], root, must_exist=True
    )
    release_dir = require_contained_path(
        accepted_root / binding["relative_directory"],
        accepted_root,
        must_exist=True,
    )
    manifest = verify_accepted_release(
        release_dir,
        accepted_root=accepted_root,
    )
    payload_bytes = sum(entry.size for entry in manifest.files)
    required_paths = {"bars.parquet", "rehabilitation_receipt.json", "source_evidence_manifest.json"}
    manifest_paths = {entry.path for entry in manifest.files}
    prospective = policy["prospective_release"]
    if (
        manifest.release_id != binding["release_id"]
        or manifest.dataset != prospective["dataset"]
        or manifest.source_epoch != prospective["source_epoch"]
        or manifest.role != prospective["role"]
        or manifest.quality_state != prospective["quality_state"]
        or manifest.row_count != policy["input_contract"]["expected_row_count"]
        or len(manifest.files) != binding["required_file_count"]
        or payload_bytes != binding["required_payload_bytes"]
        or not required_paths <= manifest_paths
    ):
        raise IntegrityError("rehabilitated accepted release differs from policy")
    unsigned = {
        "schema_version": 2,
        "project": "US_stocks_swing_model_v2",
        "mode": RETIRED_MODE,
        "policy_id": policy_id,
        "accepted_release": {
            "directory": release_dir.relative_to(root).as_posix(),
            "release_id": manifest.release_id,
            "dataset": manifest.dataset,
            "role": manifest.role,
            "quality_state": manifest.quality_state,
            "row_count": manifest.row_count,
            "file_count": len(manifest.files),
            "payload_bytes": payload_bytes,
        },
        "legacy_universe_boundary": policy["legacy_universe_boundary"],
        "evidence_boundary": policy["evidence_boundary"],
        "prospective_release": prospective,
        "authorities": policy["authorities"],
        "stop_conditions": policy["stop_conditions"],
    }
    return {
        **unsigned,
        "verification_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }
