"""Bounded, read-only historical-foundation inventory.

This command deliberately has no write, network, credential, outcome, label,
evaluation, or holdout capability.  It reads only the paths and metadata classes
declared by a content-addressed plan and emits one JSON object to stdout.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq


PROJECT = "US_stocks_swing_model_v2"
EXPECTED_ROOT = Path(r"C:\Users\donny\Desktop\US_stocks_swing_model_v2")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
DENIED_DATASETS = {
    "alpaca_discovery_proxy_outcomes",
    "alpaca_discovery_joined_trial_inputs",
}
DENIED_PATH_PREFIXES = (
    "data/w/alpaca_discovery_proxy_outcomes/",
    "data/w/alpaca_discovery_joined_trial_input/",
    "data/w/alpaca_discovery_joined_trial_inputs/",
    "data/w/o/",
    "data/w/r/",
    "reports/generated/",
)
DENIED_BASENAMES = {"api.env"}
DENIED_PARQUET_FIELDS = {
    "forward_return",
    "future_return",
    "label",
    "outcome",
    "realized_return",
    "strategy_return",
    "pnl",
}


class AssessmentError(RuntimeError):
    pass


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _as_json_value(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": _sha256_bytes(value)}
    if hasattr(value, "as_py"):
        return _as_json_value(value.as_py())
    if isinstance(value, dict):
        return {str(key): _as_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_json_value(item) for item in value]
    return value


def _require_plain_ancestor(path: Path, root: Path) -> None:
    current = path
    while True:
        stat = os.lstat(current)
        if os.path.islink(current) or (
            getattr(stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise AssessmentError(f"linked or reparse path is prohibited: {current}")
        if current == root:
            return
        current = current.parent


def _safe_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    if type(relative) is not str or not relative or "\\" in relative:
        raise AssessmentError("plan paths must be nonempty canonical POSIX-relative text")
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise AssessmentError(f"plan path escapes repository: {relative}")
    normalized = rel.as_posix()
    if normalized in DENIED_BASENAMES or Path(normalized).name in DENIED_BASENAMES:
        raise AssessmentError(f"secret path is prohibited: {relative}")
    if any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in DENIED_PATH_PREFIXES
    ):
        raise AssessmentError(f"outcome/evaluation path is prohibited: {relative}")
    candidate = root.joinpath(*rel.parts)
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AssessmentError(f"plan path resolves outside repository: {relative}") from exc
    _require_plain_ancestor(resolved, root)
    if directory != resolved.is_dir():
        kind = "directory" if directory else "file"
        raise AssessmentError(f"plan path is not the required {kind}: {relative}")
    return resolved


class ReadBudget:
    def __init__(self, maximum_bytes: int) -> None:
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            raise AssessmentError("maximum read bytes must be a positive integer")
        self.maximum_bytes = maximum_bytes
        self.bytes_read = 0
        self.files_read = 0

    def read(self, path: Path, *, maximum_file_bytes: int) -> bytes:
        size = path.stat().st_size
        if size > maximum_file_bytes:
            raise AssessmentError(f"file exceeds its read bound: {path}")
        if self.bytes_read + size > self.maximum_bytes:
            raise AssessmentError("assessment total read-byte bound would be exceeded")
        value = path.read_bytes()
        if len(value) != size:
            raise AssessmentError(f"file size changed during assessment: {path}")
        self.bytes_read += len(value)
        self.files_read += 1
        return value


def _load_json(
    path: Path,
    *,
    budget: ReadBudget,
    maximum_file_bytes: int,
) -> object:
    try:
        return json.loads(
            budget.read(path, maximum_file_bytes=maximum_file_bytes).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssessmentError(f"invalid UTF-8 JSON: {path}") from exc


def _release_manifest_census(
    root: Path,
    spec: dict[str, object],
    budget: ReadBudget,
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    accepted = _safe_path(root, str(spec["accepted_root"]), directory=True)
    allowlist = spec.get("dataset_allowlist")
    if not isinstance(allowlist, list) or not allowlist:
        raise AssessmentError("release manifest census requires a dataset allowlist")
    if any(type(item) is not str or item in DENIED_DATASETS for item in allowlist):
        raise AssessmentError("release manifest allowlist contains a denied dataset")
    maximum_releases = spec.get("maximum_releases")
    if type(maximum_releases) is not int or maximum_releases < 1:
        raise AssessmentError("release manifest bound is invalid")
    rows: list[dict[str, object]] = []
    declarations: dict[str, dict[str, object]] = {}
    for dataset in sorted(allowlist):
        dataset_path = accepted / dataset
        if not dataset_path.is_dir():
            rows.append({"dataset": dataset, "state": "ABSENT"})
            continue
        _require_plain_ancestor(dataset_path, root)
        for release in sorted(dataset_path.iterdir(), key=lambda item: item.name):
            if not release.is_dir() or release.name == ".locks":
                continue
            manifest_path = release / "release_manifest.json"
            if not manifest_path.is_file():
                rows.append(
                    {
                        "dataset": dataset,
                        "release_directory": release.name,
                        "state": "MANIFEST_ABSENT",
                    }
                )
                continue
            _require_plain_ancestor(manifest_path, root)
            payload = _load_json(
                manifest_path,
                budget=budget,
                maximum_file_bytes=int(spec["maximum_manifest_bytes"]),
            )
            if not isinstance(payload, dict):
                raise AssessmentError(f"release manifest is not an object: {manifest_path}")
            declared_dataset = payload.get("dataset")
            if declared_dataset != dataset:
                raise AssessmentError(f"release manifest dataset differs: {manifest_path}")
            files = payload.get("files")
            if not isinstance(files, list):
                raise AssessmentError(f"release manifest files differ: {manifest_path}")
            release_relative = release.relative_to(root).as_posix()
            for item in files:
                if not isinstance(item, dict) or type(item.get("path")) is not str:
                    raise AssessmentError(f"release manifest file entry differs: {manifest_path}")
                declared_relative = f"{release_relative}/{item['path']}"
                if declared_relative in declarations:
                    raise AssessmentError("release manifest file declaration is duplicated")
                declarations[declared_relative] = {
                    "sha256": item.get("sha256"),
                    "size": item.get("size"),
                    "release_id": payload.get("release_id"),
                }
            rows.append(
                {
                    "dataset": dataset,
                    "release_directory": release.name,
                    "release_id": payload.get("release_id"),
                    "source_epoch": payload.get("source_epoch"),
                    "role": payload.get("role"),
                    "quality_state": payload.get("quality_state"),
                    "created_at": payload.get("created_at"),
                    "row_count": payload.get("row_count"),
                    "event_start": payload.get("event_start"),
                    "event_end": payload.get("event_end"),
                    "schema_fingerprint": payload.get("schema_fingerprint"),
                    "code_hash": payload.get("code_hash"),
                    "config_hash": payload.get("config_hash"),
                    "environment_hash": payload.get("environment_hash"),
                    "upstream_release_ids": payload.get("upstream_release_ids"),
                    "declared_file_count": len(files),
                    "declared_bytes": sum(
                        int(item.get("size", 0))
                        for item in files
                        if isinstance(item, dict)
                    ),
                    "state": "MANIFEST_INVENTORIED_NOT_REVERIFIED",
                }
            )
            if len(rows) > maximum_releases:
                raise AssessmentError("release manifest census exceeds plan bound")
    return rows, declarations


def _identity_summary(
    root: Path,
    spec: dict[str, object],
    budget: ReadBudget,
) -> dict[str, object]:
    path = _safe_path(root, str(spec["path"]))
    raw = budget.read(path, maximum_file_bytes=int(spec["maximum_file_bytes"]))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssessmentError("identity payload is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), list):
        raise AssessmentError("identity payload shape differs")
    snapshots = payload["snapshots"]
    rows: list[dict[str, object]] = []
    snapshot_summaries: list[dict[str, object]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("rows"), list):
            raise AssessmentError("identity snapshot shape differs")
        snapshot_rows = snapshot["rows"]
        rows.extend(item for item in snapshot_rows if isinstance(item, dict))
        snapshot_summaries.append(
            {
                "snapshot_id": snapshot.get("snapshot_id"),
                "effective_at": snapshot.get("effective_at"),
                "known_at": snapshot.get("known_at"),
                "complete_membership": snapshot.get("complete_membership"),
                "evidence_state": snapshot.get("evidence_state"),
                "row_count": len(snapshot_rows),
            }
        )
    sample_count = int(spec["maximum_sample_rows"])
    sample_fields = tuple(str(item) for item in spec["sample_fields"])
    samples = [
        {field: row.get(field) for field in sample_fields}
        for row in rows[:sample_count]
    ]
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_bytes(raw),
        "schema_version": payload.get("schema_version"),
        "snapshot_count": len(snapshots),
        "snapshots": snapshot_summaries,
        "row_count": len(rows),
        "unique_asset_ids": len({row.get("asset_id") for row in rows}),
        "unique_symbols": len({row.get("symbol") for row in rows}),
        "security_types": dict(sorted(Counter(str(row.get("security_type")) for row in rows).items())),
        "exchanges": dict(sorted(Counter(str(row.get("listing_exchange")) for row in rows).items())),
        "membership_present": sum(row.get("membership_present") is True for row in rows),
        "eligible": sum(row.get("eligible") is True for row in rows),
        "samples": samples,
    }


def _parquet_probe(
    root: Path,
    spec: dict[str, object],
    declarations: dict[str, dict[str, object]],
) -> dict[str, object]:
    path = _safe_path(root, str(spec["path"]))
    parquet = pq.ParquetFile(path)
    names = tuple(parquet.schema_arrow.names)
    poisoned = sorted(set(names) & DENIED_PARQUET_FIELDS)
    if poisoned:
        raise AssessmentError(f"parquet probe exposes denied fields: {poisoned}")
    requested = tuple(str(item) for item in spec.get("sample_columns", []))
    if any(name not in names for name in requested):
        raise AssessmentError(f"parquet sample column is absent: {path}")
    maximum_rows = int(spec.get("maximum_sample_rows", 0))
    samples: list[dict[str, object]] = []
    if maximum_rows:
        for batch in parquet.iter_batches(
            batch_size=maximum_rows,
            columns=list(requested),
        ):
            samples.extend(
                {
                    name: _as_json_value(batch.column(index)[row_index])
                    for index, name in enumerate(requested)
                }
                for row_index in range(min(maximum_rows, batch.num_rows))
            )
            break
    metadata = parquet.metadata
    relative = path.relative_to(root).as_posix()
    declaration = declarations.get(relative)
    if declaration is None:
        raise AssessmentError(f"parquet probe lacks an inventoried manifest declaration: {relative}")
    if declaration.get("size") != path.stat().st_size:
        raise AssessmentError(f"parquet probe size differs from its manifest: {relative}")
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "declared_sha256": declaration.get("sha256"),
        "declaring_release_id": declaration.get("release_id"),
        "hash_recomputed": False,
        "schema": str(parquet.schema_arrow),
        "row_count": metadata.num_rows,
        "row_group_count": metadata.num_row_groups,
        "created_by": metadata.created_by,
        "samples": samples,
    }


def _receipt_census(
    root: Path,
    spec: dict[str, object],
    budget: ReadBudget,
) -> dict[str, object]:
    directory = _safe_path(root, str(spec["directory"]), directory=True)
    paths = sorted(directory.rglob("*.json"))
    maximum_files = int(spec["maximum_files"])
    if len(paths) > maximum_files:
        raise AssessmentError(f"receipt census exceeds file bound: {directory}")
    field_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    evidence_counts: Counter[str] = Counter()
    time_values: dict[str, list[str]] = {
        field: [] for field in spec.get("time_fields", [])
    }
    for path in paths:
        _require_plain_ancestor(path, root)
        payload = _load_json(
            path,
            budget=budget,
            maximum_file_bytes=int(spec["maximum_file_bytes"]),
        )
        if not isinstance(payload, dict):
            raise AssessmentError(f"receipt is not an object: {path}")
        field_counts.update(payload)
        for field, counter in (
            ("source", source_counts),
            ("status", status_counts),
            ("evidence_class", evidence_counts),
            ("validation_state", status_counts),
            ("http_status", status_counts),
        ):
            if field in payload:
                counter[str(payload[field])] += 1
        for field in time_values:
            value = payload.get(field)
            if isinstance(value, str):
                time_values[field].append(value)
    return {
        "directory": directory.relative_to(root).as_posix(),
        "json_file_count": len(paths),
        "field_presence": dict(sorted(field_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "evidence_class_counts": dict(sorted(evidence_counts.items())),
        "time_ranges": {
            field: {"minimum": min(values), "maximum": max(values)}
            if values
            else {"minimum": None, "maximum": None}
            for field, values in sorted(time_values.items())
        },
    }


def _structured_object_census(
    root: Path,
    spec: dict[str, object],
    budget: ReadBudget,
) -> dict[str, object]:
    directory = _safe_path(root, str(spec["directory"]), directory=True)
    paths = sorted(directory.rglob("*.bin"))
    if len(paths) > int(spec["maximum_files"]):
        raise AssessmentError(f"structured-object census exceeds file bound: {directory}")
    fmt = spec["format"]
    schemas: Counter[tuple[str, ...]] = Counter()
    total_rows = 0
    date_values: list[str] = []
    type_values: Counter[str] = Counter()
    known_case_hits: Counter[str] = Counter()
    known_cases = {str(value) for value in spec.get("known_cases", [])}
    for path in paths:
        raw = budget.read(path, maximum_file_bytes=int(spec["maximum_file_bytes"]))
        if fmt == "csv":
            try:
                reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
                records = list(reader)
            except UnicodeDecodeError as exc:
                raise AssessmentError(f"structured CSV is not UTF-8: {path}") from exc
            schema = tuple(reader.fieldnames or ())
        elif fmt == "json":
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AssessmentError(f"structured JSON is invalid: {path}") from exc
            if isinstance(payload, dict):
                if isinstance(payload.get("corporate_actions"), dict):
                    records = [
                        item
                        for group, items in payload["corporate_actions"].items()
                        if isinstance(items, list)
                        for item in items
                        if isinstance(item, dict)
                    ]
                    for group, items in payload["corporate_actions"].items():
                        if isinstance(items, list):
                            type_values[str(group)] += len(items)
                elif isinstance(payload.get("data"), list):
                    records = [item for item in payload["data"] if isinstance(item, dict)]
                else:
                    records = [payload]
            elif isinstance(payload, list):
                records = [item for item in payload if isinstance(item, dict)]
            else:
                records = []
            schema = tuple(sorted({key for row in records for key in row}))
        else:
            raise AssessmentError("structured-object format differs")
        schemas[schema] += 1
        total_rows += len(records)
        for row in records:
            for field in spec.get("date_fields", []):
                value = row.get(field)
                if isinstance(value, str) and value:
                    date_values.append(value)
            for field in spec.get("type_fields", []):
                value = row.get(field)
                if isinstance(value, str) and value:
                    type_values[value] += 1
            row_text = "|".join(str(value).upper() for value in row.values())
            for case in known_cases:
                if case.upper() in row_text:
                    known_case_hits[case] += 1
    return {
        "directory": directory.relative_to(root).as_posix(),
        "format": fmt,
        "object_count": len(paths),
        "row_count": total_rows,
        "schemas": [
            {"fields": list(fields), "object_count": count}
            for fields, count in sorted(schemas.items())
        ],
        "date_range": {
            "minimum": min(date_values) if date_values else None,
            "maximum": max(date_values) if date_values else None,
        },
        "type_counts": dict(sorted(type_values.items())),
        "known_case_hits": dict(sorted(known_case_hits.items())),
    }


def _validate_plan(root: Path, payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise AssessmentError("assessment plan must be a JSON object")
    plan_id = payload.get("plan_id")
    unsigned = {key: value for key, value in payload.items() if key != "plan_id"}
    if plan_id != _sha256_bytes(_canonical_bytes(unsigned)):
        raise AssessmentError("assessment plan ID differs")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("mode") != "READ_ONLY_HISTORICAL_FOUNDATION_ASSESSMENT"
        or payload.get("invocation_limit") != 1
        or payload.get("network_requests") != 0
        or payload.get("writes_allowed") is not False
        or payload.get("credential_access") is not False
        or payload.get("outcome_access") is not False
        or payload.get("label_access") is not False
        or payload.get("training") is not False
        or payload.get("evaluation") is not False
        or payload.get("backtesting") is not False
    ):
        raise AssessmentError("assessment plan authority boundary differs")
    if root != EXPECTED_ROOT.resolve(strict=True):
        raise AssessmentError("repository identity differs")
    return payload


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    return parser.parse_args(tuple(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve(strict=True).parents[1]
    _require_plain_ancestor(root, root)
    plan_path = _safe_path(root, args.plan)
    try:
        raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssessmentError("assessment plan is unreadable") from exc
    plan = _validate_plan(root, raw_plan)
    budget = ReadBudget(int(plan["maximum_total_content_bytes"]))
    parquet_specs = [dict(spec) for spec in plan["parquet_probes"]]
    parquet_paths = [_safe_path(root, str(spec["path"])) for spec in parquet_specs]
    parquet_source_bytes = sum(path.stat().st_size for path in parquet_paths)
    if parquet_source_bytes > int(plan["maximum_parquet_source_bytes"]):
        raise AssessmentError("parquet source-byte bound would be exceeded")
    release_manifests, declarations = _release_manifest_census(
        root,
        dict(plan["release_manifest_census"]),
        budget,
    )
    result = {
        "schema_version": 1,
        "project": PROJECT,
        "mode": plan["mode"],
        "plan_id": plan["plan_id"],
        "claims": {
            "read_only": True,
            "network_requests": 0,
            "credentials_accessed": False,
            "outcomes_accessed": False,
            "labels_accessed": False,
            "training_performed": False,
            "evaluation_performed": False,
            "backtesting_performed": False,
        },
        "release_manifests": release_manifests,
        "identity": _identity_summary(root, dict(plan["identity_probe"]), budget),
        "parquet_probes": [
            _parquet_probe(root, spec, declarations)
            for spec in parquet_specs
        ],
        "receipt_censuses": [
            _receipt_census(root, dict(spec), budget)
            for spec in plan["receipt_censuses"]
        ],
        "structured_object_censuses": [
            _structured_object_census(root, dict(spec), budget)
            for spec in plan["structured_object_censuses"]
        ],
    }
    result["read_budget"] = {
        "maximum_bytes": budget.maximum_bytes,
        "bytes_read": budget.bytes_read,
        "files_read": budget.files_read,
        "maximum_parquet_source_bytes": int(plan["maximum_parquet_source_bytes"]),
        "parquet_source_bytes": parquet_source_bytes,
    }
    result["result_id"] = _sha256_bytes(_canonical_bytes(result))
    sys.stdout.buffer.write(_canonical_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssessmentError as exc:
        print(f"ASSESSMENT_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
