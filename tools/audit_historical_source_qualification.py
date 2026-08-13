"""One-shot, read-only V1 historical-source evidence audit.

The command is intentionally unable to write, use credentials, call a network,
open an outcome namespace, train, evaluate, or backtest. It verifies only the
exact source packages and raw object directories named by a content-addressed
plan and emits one canonical JSON object to stdout.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pyarrow.parquet as pq


CODE_ROOT = Path(__file__).resolve(strict=True).parents[1]
sys.path.insert(0, str(CODE_ROOT / "src"))

from us_stocks_swing_model_v2.common import (  # noqa: E402
    canonical_json_bytes,
    reject_link,
    require_contained_path,
    sha256_bytes,
    sha256_file,
)
from us_stocks_swing_model_v2.releases import (  # noqa: E402
    ReleaseManifest,
    verify_accepted_release,
)


PROJECT = "US_stocks_swing_model_v2"
MODE = "READ_ONLY_HISTORICAL_SOURCE_QUALIFICATION_AUDIT"
DENIED_COMPONENTS = {
    "api.env",
    "alpaca_discovery_joined_trial_input",
    "alpaca_discovery_joined_trial_inputs",
    "alpaca_discovery_proxy_outcomes",
    "backtest",
    "evaluation",
    "holdout",
    "labels",
    "outcomes",
    "reports",
}
BAR_COLUMNS = (
    "asset_id",
    "close",
    "evidence_class",
    "high",
    "historical_membership_proven",
    "input_quality_state",
    "low",
    "open",
    "point_in_time_safe",
    "provider_symbol",
    "security_type",
    "session",
    "volume",
)


class AuditError(RuntimeError):
    pass


def _canonical_plan_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _plain_root(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    reject_link(resolved)
    if not resolved.is_dir():
        raise AuditError(f"audit root is not a directory: {resolved}")
    return resolved


def _safe_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    if type(relative) is not str or not relative or "\\" in relative:
        raise AuditError("audit paths must be canonical POSIX-relative text")
    parts = tuple(relative.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise AuditError(f"audit path is unsafe: {relative}")
    lowered = {part.casefold() for part in parts}
    if lowered & DENIED_COMPONENTS:
        raise AuditError(f"audit path names a denied component: {relative}")
    candidate = root.joinpath(*parts)
    try:
        require_contained_path(candidate, root)
    except Exception as exc:
        raise AuditError(f"audit path is not a contained plain path: {relative}") from exc
    if candidate.is_dir() != directory:
        expected = "directory" if directory else "file"
        raise AuditError(f"audit path is not the expected {expected}: {relative}")
    return candidate


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AuditError(f"read-only git query failed: {' '.join(arguments)}")
    return completed.stdout.strip()


def _validate_plan(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError("audit plan is missing or invalid JSON") from exc
    if type(payload) is not dict:
        raise AuditError("audit plan must be an object")
    plan_id = payload.get("plan_id")
    unsigned = {key: value for key, value in payload.items() if key != "plan_id"}
    if plan_id != _sha256(_canonical_plan_bytes(unsigned)):
        raise AuditError("audit plan ID differs from its content")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("mode") != MODE
        or payload.get("invocation_limit") != 1
        or payload.get("writes_allowed") is not False
        or payload.get("network_requests") != 0
        or payload.get("credential_access") is not False
        or payload.get("outcome_access") is not False
        or payload.get("label_access") is not False
        or payload.get("training") is not False
        or payload.get("evaluation") is not False
        or payload.get("backtesting") is not False
    ):
        raise AuditError("audit authority boundary differs")
    if CODE_ROOT.as_posix().casefold() != str(payload.get("code_root")).casefold():
        raise AuditError("audit code-root identity differs")
    implementation_commit = str(payload.get("implementation_commit"))
    ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(CODE_ROOT),
            "merge-base",
            "--is-ancestor",
            implementation_commit,
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if ancestor.returncode != 0:
        raise AuditError("audit implementation commit is not in current history")
    if _git(CODE_ROOT, "branch", "--show-current") != payload.get(
        "expected_code_branch"
    ):
        raise AuditError("audit code branch differs")
    if _git(CODE_ROOT, "status", "--porcelain"):
        raise AuditError("audit code worktree must be clean")
    result_path = CODE_ROOT.joinpath(*str(payload["result_record_path"]).split("/"))
    if result_path.exists():
        raise AuditError("audit plan invocation is already spent")
    return payload


def _verify_checkout(
    evidence_root: Path,
    plan: dict[str, object],
) -> dict[str, object]:
    expected = plan["evidence_checkout"]
    if not isinstance(expected, dict):
        raise AuditError("evidence checkout contract is invalid")
    branch = _git(evidence_root, "branch", "--show-current")
    head = _git(evidence_root, "rev-parse", "HEAD")
    status = _git(evidence_root, "status", "--porcelain")
    if (
        branch != expected.get("branch")
        or head != expected.get("head")
        or status
    ):
        raise AuditError("scheduler evidence checkout differs from the plan")
    hashes: dict[str, str] = {}
    expected_hashes = expected.get("capture_hashes")
    if not isinstance(expected_hashes, dict):
        raise AuditError("capture hash census is invalid")
    for relative, expected_hash in sorted(expected_hashes.items()):
        path = _safe_path(evidence_root, relative)
        actual = sha256_file(path)
        if actual != expected_hash:
            raise AuditError(f"capture baseline hash differs: {relative}")
        hashes[relative] = actual
    return {
        "branch": branch,
        "head": head,
        "worktree_clean": True,
        "capture_hashes": hashes,
    }


def _manifest_summary(manifest: ReleaseManifest) -> dict[str, object]:
    return {
        "dataset": manifest.dataset,
        "release_id": manifest.release_id,
        "source_epoch": manifest.source_epoch,
        "role": manifest.role,
        "quality_state": manifest.quality_state,
        "row_count": manifest.row_count,
        "event_start": manifest.event_start,
        "event_end": manifest.event_end,
        "file_count": len(manifest.files),
        "payload_bytes": sum(item.size for item in manifest.files),
        "schema_fingerprint": manifest.schema_fingerprint,
        "files_verified": True,
    }


def _verify_releases(
    evidence_root: Path,
    plan: dict[str, object],
) -> tuple[dict[str, object], dict[str, tuple[Path, ReleaseManifest]]]:
    accepted_root = _safe_path(
        evidence_root,
        str(plan["accepted_root"]),
        directory=True,
    )
    specs = plan.get("releases")
    if not isinstance(specs, list) or not specs:
        raise AuditError("release audit census is empty")
    preflight_bytes = 0
    selected: dict[str, tuple[Path, ReleaseManifest]] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            raise AuditError("release audit entry is invalid")
        relative = str(spec["path"])
        directory = _safe_path(evidence_root, relative, directory=True)
        try:
            manifest_payload = json.loads(
                (directory / "release_manifest.json").read_text(encoding="utf-8")
            )
            declared = ReleaseManifest.from_dict(manifest_payload)
        except Exception as exc:
            raise AuditError(f"release manifest preflight failed: {relative}") from exc
        if (
            declared.dataset != spec.get("dataset")
            or declared.release_id != spec.get("release_id")
        ):
            raise AuditError(f"release identity differs: {relative}")
        preflight_bytes += sum(item.size for item in declared.files)
        selected[declared.dataset] = (directory, declared)
    maximum = int(plan["maximum_release_payload_bytes"])
    if preflight_bytes > maximum:
        raise AuditError("release payload byte limit would be exceeded")
    summaries: dict[str, object] = {}
    verified: dict[str, tuple[Path, ReleaseManifest]] = {}
    for dataset, (directory, _declared) in sorted(selected.items()):
        try:
            manifest = verify_accepted_release(
                directory,
                accepted_root=accepted_root,
            )
        except Exception as exc:
            raise AuditError(f"accepted release verification failed: {dataset}") from exc
        summaries[dataset] = _manifest_summary(manifest)
        verified[dataset] = (directory, manifest)
    return (
        {
            "preflight_payload_bytes": preflight_bytes,
            "maximum_release_payload_bytes": maximum,
            "releases": summaries,
        },
        verified,
    )


def _identity_census(directory: Path, manifest: ReleaseManifest) -> dict[str, object]:
    path = directory / "identity_snapshots.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError("identity payload is unreadable") from exc
    snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
    if not isinstance(snapshots, list) or not snapshots:
        raise AuditError("identity snapshot census is empty")
    rows = [
        row
        for snapshot in snapshots
        if isinstance(snapshot, dict)
        for row in snapshot.get("rows", [])
        if isinstance(row, dict)
    ]
    if len(rows) != manifest.row_count:
        raise AuditError("identity row count differs from release manifest")
    asset_ids = [row.get("asset_id") for row in rows]
    symbols = [row.get("symbol") for row in rows]
    return {
        "snapshot_count": len(snapshots),
        "row_count": len(rows),
        "unique_stable_asset_ids": len(set(asset_ids)),
        "unique_symbols": len(set(symbols)),
        "duplicate_asset_ids": len(asset_ids) - len(set(asset_ids)),
        "duplicate_symbols": len(symbols) - len(set(symbols)),
        "membership_present": sum(row.get("membership_present") is True for row in rows),
        "eligible": sum(row.get("eligible") is True for row in rows),
        "active": sum(row.get("active") is True for row in rows),
        "security_type_counts": dict(
            sorted(Counter(str(row.get("security_type")) for row in rows).items())
        ),
        "snapshot_effective_times": sorted(
            {
                str(snapshot.get("effective_at"))
                for snapshot in snapshots
                if isinstance(snapshot, dict)
            }
        ),
        "snapshot_known_times": sorted(
            {
                str(snapshot.get("known_at"))
                for snapshot in snapshots
                if isinstance(snapshot, dict)
            }
        ),
        "historical_snapshot_count_before_2026": sum(
            str(snapshot.get("effective_at", "")).startswith(tuple(str(year) for year in range(1900, 2026)))
            for snapshot in snapshots
            if isinstance(snapshot, dict)
        ),
    }


def _counter_update(counter: Counter[str], values: list[object]) -> None:
    counter.update("<NULL>" if value is None else str(value) for value in values)


def _bar_census(directory: Path, manifest: ReleaseManifest) -> dict[str, object]:
    parquet_paths = sorted((directory / "bars").glob("year=*.parquet"))
    if not parquet_paths:
        raise AuditError("historical bar release has no Parquet partitions")
    total_rows = 0
    unique_assets: set[str] = set()
    unique_symbols: set[str] = set()
    type_counts: Counter[str] = Counter()
    evidence_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    point_in_time_true = 0
    historical_membership_true = 0
    invalid_ohlc = 0
    invalid_volume = 0
    negative_volume = 0
    zero_volume = 0
    session_min: date | None = None
    session_max: date | None = None
    schemas: set[tuple[str, ...]] = set()
    partitions: list[dict[str, object]] = []
    for path in parquet_paths:
        parquet = pq.ParquetFile(path)
        fields = tuple(parquet.schema_arrow.names)
        schemas.add(fields)
        missing = set(BAR_COLUMNS) - set(fields)
        if missing:
            raise AuditError(f"historical bar partition omits columns: {sorted(missing)}")
        rows = parquet.metadata.num_rows
        total_rows += rows
        partitions.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "rows": rows,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "row_groups": parquet.metadata.num_row_groups,
            }
        )
        for batch in parquet.iter_batches(batch_size=131072, columns=list(BAR_COLUMNS)):
            names = batch.schema.names
            column = lambda name: batch.column(names.index(name))
            asset_values = column("asset_id").to_pylist()
            symbol_values = column("provider_symbol").to_pylist()
            unique_assets.update(str(value) for value in asset_values if value is not None)
            unique_symbols.update(str(value) for value in symbol_values if value is not None)
            _counter_update(type_counts, column("security_type").to_pylist())
            _counter_update(evidence_counts, column("evidence_class").to_pylist())
            _counter_update(quality_counts, column("input_quality_state").to_pylist())
            point_in_time_true += sum(
                value is True for value in column("point_in_time_safe").to_pylist()
            )
            historical_membership_true += sum(
                value is True
                for value in column("historical_membership_proven").to_pylist()
            )
            opens = np.asarray(column("open").to_numpy(zero_copy_only=False), dtype=float)
            highs = np.asarray(column("high").to_numpy(zero_copy_only=False), dtype=float)
            lows = np.asarray(column("low").to_numpy(zero_copy_only=False), dtype=float)
            closes = np.asarray(column("close").to_numpy(zero_copy_only=False), dtype=float)
            volumes = np.asarray(column("volume").to_numpy(zero_copy_only=False), dtype=float)
            valid = (
                np.isfinite(opens)
                & np.isfinite(highs)
                & np.isfinite(lows)
                & np.isfinite(closes)
                & (opens > 0)
                & (highs > 0)
                & (lows > 0)
                & (closes > 0)
                & (lows <= opens)
                & (lows <= closes)
                & (opens <= highs)
                & (closes <= highs)
            )
            invalid_ohlc += int((~valid).sum())
            invalid_volume += int((~np.isfinite(volumes)).sum())
            negative_volume += int((volumes < 0).sum())
            zero_volume += int((volumes == 0).sum())
            sessions = [value for value in column("session").to_pylist() if value is not None]
            if sessions:
                current_min = min(sessions)
                current_max = max(sessions)
                session_min = current_min if session_min is None else min(session_min, current_min)
                session_max = current_max if session_max is None else max(session_max, current_max)
    if total_rows != manifest.row_count:
        raise AuditError("historical bar partition rows differ from release manifest")
    return {
        "partition_count": len(parquet_paths),
        "row_count": total_rows,
        "unique_asset_ids": len(unique_assets),
        "unique_provider_symbols": len(unique_symbols),
        "session_start": session_min.isoformat() if session_min else None,
        "session_end": session_max.isoformat() if session_max else None,
        "schema_variants": len(schemas),
        "security_type_counts": dict(sorted(type_counts.items())),
        "evidence_class_counts": dict(sorted(evidence_counts.items())),
        "input_quality_state_counts": dict(sorted(quality_counts.items())),
        "point_in_time_safe_true_rows": point_in_time_true,
        "historical_membership_proven_true_rows": historical_membership_true,
        "invalid_ohlc_rows": invalid_ohlc,
        "invalid_volume_rows": invalid_volume,
        "negative_volume_rows": negative_volume,
        "zero_volume_rows": zero_volume,
        "duplicate_security_date_check": "NOT_RUN_SOURCE_SEMANTICALLY_QUARANTINED",
        "missing_session_check": "BLOCKED_NO_QUALIFIED_HISTORICAL_LISTING_INTERVALS",
        "partitions": partitions,
    }


def _calendar_census(directory: Path, manifest: ReleaseManifest) -> dict[str, object]:
    path = directory / "sessions.parquet"
    table = pq.read_table(
        path,
        columns=[
            "session",
            "open_at",
            "close_at",
            "early_close",
            "calendar_name",
            "calendar_package",
            "calendar_version",
        ],
    )
    if table.num_rows != manifest.row_count:
        raise AuditError("calendar rows differ from release manifest")
    sessions = table.column("session").to_pylist()
    opens = table.column("open_at").to_pylist()
    closes = table.column("close_at").to_pylist()
    return {
        "row_count": table.num_rows,
        "session_start": min(sessions).isoformat(),
        "session_end": max(sessions).isoformat(),
        "duplicate_sessions": len(sessions) - len(set(sessions)),
        "invalid_boundaries": sum(
            opened is None or closed is None or opened >= closed
            for opened, closed in zip(opens, closes, strict=True)
        ),
        "early_close_sessions": sum(
            value is True for value in table.column("early_close").to_pylist()
        ),
        "calendar_names": sorted(set(table.column("calendar_name").to_pylist())),
        "calendar_packages": sorted(set(table.column("calendar_package").to_pylist())),
        "calendar_versions": sorted(set(table.column("calendar_version").to_pylist())),
        "sha256": sha256_file(path),
    }


def _structured_census(
    evidence_root: Path,
    spec: dict[str, object],
    *,
    maximum_total_bytes: int,
) -> tuple[dict[str, object], int]:
    directory = _safe_path(evidence_root, str(spec["directory"]), directory=True)
    paths = sorted(directory.rglob("*.bin"))
    if len(paths) > int(spec["maximum_files"]):
        raise AuditError("structured source exceeds its file-count bound")
    total_bytes = sum(path.stat().st_size for path in paths)
    if total_bytes > maximum_total_bytes:
        raise AuditError("structured source exceeds its byte bound")
    schemas: Counter[tuple[str, ...]] = Counter()
    type_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    dates: list[str] = []
    total_rows = 0
    hashes: list[str] = []
    for path in paths:
        if path.stat().st_size > int(spec["maximum_file_bytes"]):
            raise AuditError("structured source file exceeds its byte bound")
        raw = path.read_bytes()
        hashes.append(_sha256(raw))
        if spec["format"] == "csv":
            try:
                reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
                records = list(reader)
            except UnicodeDecodeError as exc:
                raise AuditError("structured CSV is not UTF-8") from exc
            schema = tuple(reader.fieldnames or ())
        elif spec["format"] == "json":
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuditError("structured JSON is invalid") from exc
            if isinstance(payload, dict) and isinstance(payload.get("corporate_actions"), dict):
                records = [
                    item
                    for group, items in payload["corporate_actions"].items()
                    if isinstance(items, list)
                    for item in items
                    if isinstance(item, dict)
                ]
                for group, items in payload["corporate_actions"].items():
                    if isinstance(items, list):
                        type_counts[str(group)] += len(items)
            elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
                records = [item for item in payload["data"] if isinstance(item, dict)]
            elif isinstance(payload, list):
                records = [item for item in payload if isinstance(item, dict)]
            elif isinstance(payload, dict):
                records = [payload]
            else:
                records = []
            schema = tuple(sorted({key for row in records for key in row}))
        else:
            raise AuditError("structured source format is invalid")
        schemas[schema] += 1
        total_rows += len(records)
        for row in records:
            for field in spec.get("date_fields", []):
                value = row.get(field)
                if isinstance(value, str) and value:
                    dates.append(value)
            for field in spec.get("type_fields", []):
                value = row.get(field)
                if isinstance(value, str) and value:
                    type_counts[value] += 1
            value = row.get("status")
            if isinstance(value, str) and value:
                status_counts[value] += 1
    return (
        {
            "source_id": spec["source_id"],
            "directory": spec["directory"],
            "format": spec["format"],
            "object_count": len(paths),
            "row_count": total_rows,
            "bytes": total_bytes,
            "content_hashes": sorted(hashes),
            "content_hash_census_id": sha256_bytes(
                canonical_json_bytes(sorted(hashes))
            ),
            "schemas": [
                {"fields": list(fields), "object_count": count}
                for fields, count in sorted(schemas.items())
            ],
            "date_start": min(dates) if dates else None,
            "date_end": max(dates) if dates else None,
            "type_counts": dict(sorted(type_counts.items())),
            "status_counts": dict(sorted(status_counts.items())),
        },
        total_bytes,
    )


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    return parser.parse_args(tuple(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    plan_path = _safe_path(CODE_ROOT, args.plan)
    plan = _validate_plan(plan_path)
    evidence_root = _plain_root(Path(str(plan["evidence_root"])))
    checkout = _verify_checkout(evidence_root, plan)
    release_summary, verified = _verify_releases(evidence_root, plan)
    identity = _identity_census(*verified["identity"])
    bars = _bar_census(*verified["alpaca_historical_daily_bars"])
    calendar = _calendar_census(*verified["xnys_sessions"])
    structured: list[dict[str, object]] = []
    structured_bytes = 0
    maximum_structured = int(plan["maximum_structured_source_bytes"])
    for raw_spec in plan["structured_sources"]:
        item, used = _structured_census(
            evidence_root,
            dict(raw_spec),
            maximum_total_bytes=maximum_structured - structured_bytes,
        )
        structured.append(item)
        structured_bytes += used
    result = {
        "schema_version": 1,
        "project": PROJECT,
        "mode": MODE,
        "plan_id": plan["plan_id"],
        "code_commit": _git(CODE_ROOT, "rev-parse", "HEAD"),
        "implementation_commit": plan["implementation_commit"],
        "claims": {
            "read_only": True,
            "network_requests": 0,
            "credentials_accessed": False,
            "outcomes_accessed": False,
            "labels_accessed": False,
            "training_performed": False,
            "evaluation_performed": False,
            "backtesting_performed": False,
            "files_written": 0,
        },
        "evidence_checkout": checkout,
        "accepted_releases": release_summary,
        "identity": identity,
        "historical_bars": bars,
        "calendar": calendar,
        "structured_sources": structured,
        "structured_source_bytes": structured_bytes,
        "source_admission_conclusion": {
            "status": "BLOCKED",
            "historical_security_master": "BLOCKED_ONE_2026_SNAPSHOT_ONLY",
            "raw_daily_ohlcv": "QUARANTINED_CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED",
            "corporate_actions": "BLOCKED_EFFECTIVE_EVENT_AND_HISTORICAL_PUBLICATION_COVERAGE_UNPROVEN",
            "delisting_terminal_events": "BLOCKED_TICKER_KEYED_RECONSTRUCTION_NOT_STABLE_ID_COMPLETE",
            "exchange_calendar": "PASS_QUALIFIED_COMPONENT",
            "canonical_panel_build_authorized": False,
        },
    }
    result["result_id"] = sha256_bytes(canonical_json_bytes(result))
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(f"SOURCE_AUDIT_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
