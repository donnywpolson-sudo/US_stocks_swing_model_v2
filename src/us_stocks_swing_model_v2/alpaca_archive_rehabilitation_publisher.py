from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .alpaca_archive_rehabilitation import (
    ArchiveExpectations,
    build_alpaca_archive_rehabilitation_plan,
    inspect_alpaca_archive,
    load_alpaca_archive_rehabilitation_policy,
)
from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .canonical.alpaca import _accept_native_bar
from .canonical.parquet import deterministic_parquet_bytes, deterministic_table
from .common import (
    assert_exact_tree,
    atomic_write_new,
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from .environment import validate_environment_lock
from .errors import ContractError, IntegrityError
from .hfdl_retirement import RETIRED_STATE, load_hfdl_retirement_policy
from .releases import (
    AtomicReleasePublisher,
    ReleaseFile,
    ReleaseManifest,
    verify_accepted_release,
)


PROJECT = "US_stocks_swing_model_v2"
PUBLICATION_POLICY_PATH = Path(
    "config/alpaca_archive_rehabilitation_publication_policy.json"
)
PUBLICATION_MODE = (
    "ALPACA_LEGACY_ARCHIVE_REHABILITATION_PUBLICATION_PLAN_ONLY_BY_DEFAULT"
)
PUBLICATION_CONFIRMATION_TOKEN = (
    "ALPACA_ARCHIVE_REHABILITATION_PUBLICATION_APPROVED"
)
PUBLICATION_CONFIRMATION_VALUE = "YES"
SYNTHETIC_PUBLICATION_SCOPE = (
    "SYNTHETIC_ALPACA_ARCHIVE_REHABILITATION_PUBLICATION"
)
DATASET = "alpaca_legacy_daily_bars"
SOURCE_EPOCH = "alpaca_sip_legacy_canonicalized_payload_20160104_20260710_v1"
ROLE = "legacy_discovery_only"
QUALITY_STATE = "LEGACY_CAVEATED"
BARS_PATH = "bars.parquet"
EVIDENCE_PATH = "source_evidence_manifest.json"
RECEIPT_PATH = "rehabilitation_receipt.json"
NATIVE_PAGE_PREFIX = "native_pages"
EVIDENCE_CLASS = "LEGACY_DISCOVERY"
PRODUCTION_STATUS = "PUBLISHED_PIT_UNRESOLVED_LEGACY_DISCOVERY"
SYNTHETIC_STATUS = "SYNTHETIC_PUBLICATION_MECHANICS_ONLY"
PROHIBITIONS = (
    "source_activation",
    "eligible_universe_construction",
    "feature_or_outcome_construction",
    "training",
    "evaluation",
    "research",
    "prospective_confirmation",
    "historical_membership_claim",
    "point_in_time_or_survivorship_safe_claim",
    "hfdl_use_or_pooling",
)

REHABILITATED_ALPACA_SCHEMA = pa.schema(
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
        ("available_at", pa.timestamp("us", tz="UTC")),
        ("source_page_sha256", pa.string()),
        ("source_epoch", pa.string()),
        ("evidence_class", pa.string()),
        ("quality_state", pa.string()),
        ("historical_proxy", pa.bool_()),
        ("point_in_time_safe", pa.bool_()),
    ]
)
SCHEMA_FINGERPRINT = sha256_bytes(
    canonical_json_bytes(str(REHABILITATED_ALPACA_SCHEMA))
)

CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/alpaca_archive_rehabilitation.py",
    "src/us_stocks_swing_model_v2/alpaca_archive_rehabilitation_publisher.py",
    "src/us_stocks_swing_model_v2/canonical/alpaca.py",
    "src/us_stocks_swing_model_v2/canonical/parquet.py",
    "src/us_stocks_swing_model_v2/cli/publish_alpaca_archive_rehabilitation.py",
    "src/us_stocks_swing_model_v2/hfdl_retirement.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    "config/alpaca_archive_rehabilitation_policy.json",
    "config/alpaca_archive_rehabilitation_publication_policy.json",
    "config/environment.lock.json",
    "config/hfdl_retirement_policy.json",
    "config/sources.json",
)


@dataclass(frozen=True)
class RehabilitationPage:
    source_path: Path
    source_relative: str
    output_relative: str
    compressed_size: int
    compressed_sha256: str
    uncompressed_size: int
    uncompressed_sha256: str

    def evidence_dict(self) -> dict[str, object]:
        return {
            "source_relative": self.source_relative,
            "output_relative": self.output_relative,
            "compressed_size": self.compressed_size,
            "compressed_sha256": self.compressed_sha256,
            "uncompressed_size": self.uncompressed_size,
            "uncompressed_sha256": self.uncompressed_sha256,
        }


@dataclass(frozen=True)
class RehabilitationCandidate:
    archive_root: Path
    assessment_id: str
    rehabilitation_policy_id: str
    hfdl_retirement_policy_id: str
    inventory: Mapping[str, Any]
    evidence_boundary: Mapping[str, Any]
    table: pa.Table
    bars_bytes: bytes
    pages: tuple[RehabilitationPage, ...]
    evidence_manifest_bytes: bytes
    candidate_id: str

    @property
    def row_count(self) -> int:
        return self.table.num_rows


@dataclass(frozen=True)
class RehabilitationPublication:
    publication_plan_id: str
    release_id: str
    receipt_id: str
    release_directory: Path
    work_directory: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _json_object(
    path: Path,
    *,
    label: str,
    require_canonical: bool = False,
) -> dict[str, Any]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(payload) is not dict:
        raise IntegrityError(f"{label} must be one JSON object")
    if require_canonical and raw != canonical_json_bytes(payload):
        raise IntegrityError(f"{label} is not canonically encoded")
    return payload


def _run_git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrityError(
            "Alpaca rehabilitation publication requires a valid Git repository"
        ) from exc
    return completed.stdout.strip()


def _repository_binding(root: Path) -> dict[str, str]:
    resolved = root.resolve(strict=True)
    git_root = Path(_run_git(resolved, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    if git_root != resolved or _run_git(resolved, "branch", "--show-current") != "main":
        raise IntegrityError(
            "Alpaca rehabilitation publication requires the exact main repository"
        )
    if _run_git(resolved, "status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError(
            "Alpaca rehabilitation publication requires a clean committed tree"
        )
    head = _run_git(resolved, "rev-parse", "HEAD")
    tree = _run_git(resolved, "rev-parse", "HEAD^{tree}")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None or re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", tree
    ) is None:
        raise IntegrityError("Alpaca rehabilitation Git identity is malformed")
    return {
        "root": str(resolved),
        "branch": "main",
        "commit": head,
        "tree": tree,
    }


def _closure(root: Path, paths: Iterable[str]) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    for relative in sorted(paths):
        path = root / relative
        require_contained_path(path, root)
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise IntegrityError(f"publication closure file is absent: {relative}")
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "files": entries,
        "closure_sha256": sha256_bytes(canonical_json_bytes(entries)),
    }


def _expectations(policy: Mapping[str, Any]) -> ArchiveExpectations:
    source = policy["input_contract"]
    return ArchiveExpectations(
        source=source["source"],
        source_route_name=source["source_route_name"],
        feed=source["feed"],
        timeframe=source["timeframe"],
        adjustment=source["adjustment"],
        request_start_date=source["request_start_date"],
        request_end_date=source["request_end_date"],
        symbol_count=source["expected_symbol_count"],
        page_count=source["expected_page_count"],
        chunk_count=source["expected_chunk_count"],
        row_count=source["expected_row_count"],
        event_start=source["expected_event_start"],
        event_end=source["expected_event_end"],
        compressed_bytes=source["expected_compressed_bytes"],
        uncompressed_bytes=source["expected_uncompressed_bytes"],
    )


def _validate_publication_policy(payload: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "policy_version",
        "project",
        "mode",
        "rehabilitation_policy_id",
        "hfdl_retirement_policy_id",
        "assessment_binding",
        "release_contract",
        "outputs",
        "production_execution",
        "synthetic_execution",
        "code_closure_paths",
        "config_closure_paths",
        "authorities",
        "stop_conditions",
    }
    if set(payload) != expected_fields:
        raise ContractError("rehabilitation publication policy fields differ")
    if (
        payload["schema_version"] != 1
        or payload["policy_version"] != "1.0.0"
        or payload["project"] != PROJECT
        or payload["mode"] != PUBLICATION_MODE
    ):
        raise ContractError("rehabilitation publication policy identity differs")
    for name in ("rehabilitation_policy_id", "hfdl_retirement_policy_id"):
        require_sha256(payload[name], f"publication_policy.{name}")
    assessment = payload["assessment_binding"]
    if (
        type(assessment) is not dict
        or set(assessment)
        != {
            "plan_id",
            "page_census_sha256",
            "provenance_census_sha256",
            "metadata_evidence_census_sha256",
            "symbols",
            "pages",
            "chunks",
            "rows",
            "event_start",
            "event_end",
            "compressed_bytes",
            "uncompressed_bytes",
        }
    ):
        raise ContractError("rehabilitation assessment binding differs")
    for name in (
        "plan_id",
        "page_census_sha256",
        "provenance_census_sha256",
        "metadata_evidence_census_sha256",
    ):
        require_sha256(assessment[name], f"assessment_binding.{name}")
    for name in (
        "symbols",
        "pages",
        "chunks",
        "rows",
        "compressed_bytes",
        "uncompressed_bytes",
    ):
        if type(assessment[name]) is not int or assessment[name] < 1:
            raise ContractError(f"assessment_binding.{name} is invalid")
    release = payload["release_contract"]
    if release != {
        "dataset": DATASET,
        "source_epoch": SOURCE_EPOCH,
        "role": ROLE,
        "quality_state": QUALITY_STATE,
        "bars_path": BARS_PATH,
        "source_evidence_manifest_path": EVIDENCE_PATH,
        "receipt_path": RECEIPT_PATH,
        "native_page_prefix": NATIVE_PAGE_PREFIX,
        "copy_exact_page_count": 198,
        "regenerate_derived_parquet": True,
        "copy_legacy_derived_parquet": False,
        "active_source_eligible": False,
        "training_authorized": False,
        "research_authorized": False,
    }:
        raise ContractError("rehabilitation release contract differs")
    outputs = payload["outputs"]
    if outputs != {
        "accepted_root": "data/vault/accepted",
        "work_root": "data/w/alpaca_archive_rehabilitation",
        "tracked": False,
        "ignored_generated_evidence": True,
        "publication_count": 1,
        "overwrite_or_replace_existing_release": False,
    }:
        raise ContractError("rehabilitation publication outputs differ")
    production = payload["production_execution"]
    if production != {
        "plan_only_default": True,
        "approved_plan_id_required": True,
        "confirmation_environment_variable": PUBLICATION_CONFIRMATION_TOKEN,
        "confirmation_value": PUBLICATION_CONFIRMATION_VALUE,
        "network_calls": 0,
        "credential_access": False,
        "retry_authorized": False,
        "cleanup_authorized": False,
    }:
        raise ContractError("rehabilitation production boundary differs")
    synthetic = payload["synthetic_execution"]
    if synthetic != {
        "permit_scope": SYNTHETIC_PUBLICATION_SCOPE,
        "fixture_root_required": True,
        "real_archive_allowed": False,
        "generated_evidence_eligible": False,
    }:
        raise ContractError("rehabilitation synthetic boundary differs")
    if (
        tuple(payload["code_closure_paths"]) != CODE_CLOSURE_PATHS
        or tuple(payload["config_closure_paths"]) != CONFIG_CLOSURE_PATHS
    ):
        raise ContractError("rehabilitation publication closure differs")
    authorities = payload["authorities"]
    if (
        type(authorities) is not dict
        or not authorities
        or any(value is not False for value in authorities.values())
    ):
        raise ContractError("rehabilitation publication policy grants authority")
    if type(payload["stop_conditions"]) is not list or not payload["stop_conditions"]:
        raise ContractError("rehabilitation publication stop conditions are absent")


def load_rehabilitation_publication_policy(
    repository_root: Path,
) -> tuple[dict[str, Any], str]:
    root = Path(repository_root).resolve(strict=True)
    policy = _json_object(
        root / PUBLICATION_POLICY_PATH,
        label="Alpaca rehabilitation publication policy",
    )
    _validate_publication_policy(policy)
    return policy, sha256_bytes(canonical_json_bytes(policy))


def _validate_inventory_binding(
    inventory: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> None:
    observed = {key: value for key, value in inventory.items() if key != "archive_root"}
    expected = {
        "symbols": binding["symbols"],
        "pages": binding["pages"],
        "chunks": binding["chunks"],
        "rows": binding["rows"],
        "event_start": binding["event_start"],
        "event_end": binding["event_end"],
        "compressed_bytes": binding["compressed_bytes"],
        "uncompressed_bytes": binding["uncompressed_bytes"],
        "provenance_census_sha256": binding["provenance_census_sha256"],
        "page_census_sha256": binding["page_census_sha256"],
        "metadata_evidence_census_sha256": binding[
            "metadata_evidence_census_sha256"
        ],
    }
    if observed != expected:
        raise IntegrityError("rehabilitation archive differs from the bound assessment")


def _read_native_page(path: Path, archive_root: Path) -> tuple[bytes, bytes, dict[str, Any]]:
    require_contained_path(path, archive_root)
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError("rehabilitation input page is not an independent plain file")
    compressed = path.read_bytes()
    try:
        raw = gzip.decompress(compressed)
    except (OSError, EOFError) as exc:
        raise ContractError("rehabilitation input page is not valid gzip") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("rehabilitation input page JSON is invalid") from exc
    if (
        type(payload) is not dict
        or set(payload) != {"bars", "next_page_token"}
        or type(payload["bars"]) is not dict
        or not payload["bars"]
        or (
            payload["next_page_token"] is not None
            and (
                type(payload["next_page_token"]) is not str
                or not payload["next_page_token"]
            )
        )
    ):
        raise ContractError("rehabilitation input page schema differs")
    return compressed, raw, payload


def build_rehabilitation_candidate(
    archive_root: Path,
    *,
    expectations: ArchiveExpectations,
    metadata_evidence_files: Iterable[str],
    assessment_id: str,
    rehabilitation_policy_id: str,
    hfdl_retirement_policy_id: str,
    evidence_boundary: Mapping[str, Any],
    expected_inventory: Mapping[str, Any] | None = None,
    _validated_inventory: Mapping[str, Any] | None = None,
) -> RehabilitationCandidate:
    """Revalidate and canonicalize one exact archive without writing any file."""

    require_sha256(assessment_id, "rehabilitation assessment_id")
    require_sha256(rehabilitation_policy_id, "rehabilitation policy_id")
    require_sha256(hfdl_retirement_policy_id, "HFDL retirement policy_id")
    root = Path(archive_root).resolve(strict=True)
    inventory = (
        dict(_validated_inventory)
        if _validated_inventory is not None
        else inspect_alpaca_archive(
            root,
            expectations=expectations,
            metadata_evidence_files=metadata_evidence_files,
        )
    )
    if expected_inventory is not None:
        _validate_inventory_binding(inventory, expected_inventory)

    page_root = root / "native" / "bars" / "raw"
    page_paths = sorted(
        page_root.glob("chunk_*/page_*.json.gz"),
        key=lambda value: value.relative_to(root).as_posix(),
    )
    if len(page_paths) != expectations.page_count:
        raise IntegrityError("rehabilitation candidate page count differs")

    eastern = ZoneInfo("America/New_York")
    seen_keys: set[tuple[str, object]] = set()
    tables: list[pa.Table] = []
    pages: list[RehabilitationPage] = []
    observed_page_census: list[dict[str, object]] = []
    for page_path in page_paths:
        compressed, raw, payload = _read_native_page(page_path, root)
        compressed_sha256 = sha256_bytes(compressed)
        source_relative = page_path.relative_to(root).as_posix()
        suffix = page_path.relative_to(page_root).as_posix()
        output_relative = f"{NATIVE_PAGE_PREFIX}/{suffix}"
        records: list[dict[str, object]] = []
        for symbol, bars in payload["bars"].items():
            if (
                type(symbol) is not str
                or not symbol
                or symbol != symbol.strip().upper()
                or type(bars) is not list
                or not bars
            ):
                raise ContractError("rehabilitation page symbol/bars shape differs")
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
                records.append(
                    {
                        "provider_symbol": symbol,
                        "asset_id": None,
                        "session": session,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "trade_count": trade_count,
                        "vwap": vwap,
                        "bar_event_at": event_at,
                        "available_at": None,
                        "source_page_sha256": compressed_sha256,
                        "source_epoch": SOURCE_EPOCH,
                        "evidence_class": EVIDENCE_CLASS,
                        "quality_state": QUALITY_STATE,
                        "historical_proxy": True,
                        "point_in_time_safe": False,
                    }
                )
        tables.append(pa.Table.from_pylist(records, schema=REHABILITATED_ALPACA_SCHEMA))
        page = RehabilitationPage(
            source_path=page_path,
            source_relative=source_relative,
            output_relative=output_relative,
            compressed_size=len(compressed),
            compressed_sha256=compressed_sha256,
            uncompressed_size=len(raw),
            uncompressed_sha256=sha256_bytes(raw),
        )
        pages.append(page)
        observed_page_census.append(
            {
                "path": source_relative,
                "compressed_size": len(compressed),
                "compressed_sha256": compressed_sha256,
                "uncompressed_size": len(raw),
                "uncompressed_sha256": sha256_bytes(raw),
            }
        )
    if sha256_bytes(canonical_json_bytes(observed_page_census)) != inventory[
        "page_census_sha256"
    ]:
        raise IntegrityError("rehabilitation candidate page census differs")

    combined = pa.concat_tables(tables)
    table = deterministic_table(
        combined,
        REHABILITATED_ALPACA_SCHEMA,
        ("provider_symbol", "session"),
    )
    if table.num_rows != expectations.row_count:
        raise IntegrityError("rehabilitation candidate row count differs")
    bars_bytes = deterministic_parquet_bytes(
        table,
        schema=REHABILITATED_ALPACA_SCHEMA,
        sort_keys=("provider_symbol", "session"),
    )
    candidate_core = {
        "schema_version": 1,
        "project": PROJECT,
        "assessment_id": assessment_id,
        "rehabilitation_policy_id": rehabilitation_policy_id,
        "hfdl_retirement_policy_id": hfdl_retirement_policy_id,
        "source_epoch": SOURCE_EPOCH,
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "bars_size": len(bars_bytes),
        "bars_sha256": sha256_bytes(bars_bytes),
        "inventory": {
            key: value for key, value in inventory.items() if key != "archive_root"
        },
        "pages": [page.evidence_dict() for page in pages],
        "evidence_boundary": dict(evidence_boundary),
        "authorities": {
            "active_source": False,
            "eligible_universe": False,
            "features_or_outcomes": False,
            "training_or_evaluation": False,
            "research": False,
            "hfdl": False,
        },
        "prohibitions": list(PROHIBITIONS),
    }
    candidate_id = sha256_bytes(canonical_json_bytes(candidate_core))
    evidence_manifest = {
        **candidate_core,
        "candidate_id": candidate_id,
        "input_kind": "canonicalized_provider_json_payload_not_original_http_bytes",
        "copied_page_count": len(pages),
        "derived_legacy_parquet_copied": False,
        "canonical_parquet_regenerated": True,
    }
    return RehabilitationCandidate(
        archive_root=root,
        assessment_id=assessment_id,
        rehabilitation_policy_id=rehabilitation_policy_id,
        hfdl_retirement_policy_id=hfdl_retirement_policy_id,
        inventory=inventory,
        evidence_boundary=dict(evidence_boundary),
        table=table,
        bars_bytes=bars_bytes,
        pages=tuple(pages),
        evidence_manifest_bytes=canonical_json_bytes(evidence_manifest),
        candidate_id=candidate_id,
    )


def _manifest_from_files(
    *,
    candidate: RehabilitationCandidate,
    receipt_bytes: bytes,
    created_at: str,
    code_hash: str,
    config_hash: str,
    environment_hash: str,
) -> ReleaseManifest:
    parse_utc_z(created_at, "rehabilitation publication created_at")
    for name, value in (
        ("code_hash", code_hash),
        ("config_hash", config_hash),
        ("environment_hash", environment_hash),
    ):
        require_sha256(value, name)
    files = [
        ReleaseFile(BARS_PATH, len(candidate.bars_bytes), sha256_bytes(candidate.bars_bytes)),
        ReleaseFile(
            EVIDENCE_PATH,
            len(candidate.evidence_manifest_bytes),
            sha256_bytes(candidate.evidence_manifest_bytes),
        ),
        ReleaseFile(RECEIPT_PATH, len(receipt_bytes), sha256_bytes(receipt_bytes)),
        *[
            ReleaseFile(
                page.output_relative,
                page.compressed_size,
                page.compressed_sha256,
            )
            for page in candidate.pages
        ],
    ]
    unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "dataset": DATASET,
        "source_epoch": SOURCE_EPOCH,
        "role": ROLE,
        "quality_state": QUALITY_STATE,
        "created_at": created_at,
        "row_count": candidate.row_count,
        "event_start": candidate.inventory["event_start"],
        "event_end": candidate.inventory["event_end"],
        "upstream_release_ids": [],
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "environment_hash": environment_hash,
        "files": [entry.as_dict() for entry in sorted(files, key=lambda item: item.path)],
    }
    manifest = ReleaseManifest(
        schema_version=1,
        project=PROJECT,
        dataset=DATASET,
        source_epoch=SOURCE_EPOCH,
        role=ROLE,
        quality_state=QUALITY_STATE,
        created_at=created_at,
        row_count=candidate.row_count,
        event_start=str(candidate.inventory["event_start"]),
        event_end=str(candidate.inventory["event_end"]),
        upstream_release_ids=(),
        schema_fingerprint=SCHEMA_FINGERPRINT,
        code_hash=code_hash,
        config_hash=config_hash,
        environment_hash=environment_hash,
        files=tuple(sorted(files, key=lambda item: item.path)),
        release_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    manifest.validate()
    return manifest


def _receipt(
    *,
    candidate: RehabilitationCandidate,
    publication_plan_id: str,
    publication_policy_id: str,
    created_at: str,
    synthetic_permit_id: str | None,
) -> dict[str, Any]:
    unsigned = {
        "schema_version": 2,
        "project": PROJECT,
        "status": (
            SYNTHETIC_STATUS
            if synthetic_permit_id is not None
            else PRODUCTION_STATUS
        ),
        "created_at": created_at,
        "publication_plan_id": publication_plan_id,
        "publication_policy_id": publication_policy_id,
        "candidate_id": candidate.candidate_id,
        "assessment_id": candidate.assessment_id,
        "rehabilitation_policy_id": candidate.rehabilitation_policy_id,
        "hfdl_retirement_policy_id": candidate.hfdl_retirement_policy_id,
        "source_epoch": SOURCE_EPOCH,
        "input_census": {
            key: value
            for key, value in candidate.inventory.items()
            if key != "archive_root"
        },
        "outputs": {
            "bars_path": BARS_PATH,
            "bars_size": len(candidate.bars_bytes),
            "bars_sha256": sha256_bytes(candidate.bars_bytes),
            "schema_fingerprint": SCHEMA_FINGERPRINT,
            "source_evidence_manifest_path": EVIDENCE_PATH,
            "source_evidence_manifest_sha256": sha256_bytes(
                candidate.evidence_manifest_bytes
            ),
            "copied_page_count": len(candidate.pages),
            "copied_page_census_sha256": sha256_bytes(
                canonical_json_bytes(
                    [page.evidence_dict() for page in candidate.pages]
                )
            ),
        },
        "evidence_boundary": dict(candidate.evidence_boundary),
        "synthetic_permit_id": synthetic_permit_id,
        "authorities": {
            "legacy_discovery_publication": synthetic_permit_id is None,
            "active_source": False,
            "eligible_universe": False,
            "features_or_outcomes": False,
            "training_or_evaluation": False,
            "research": False,
            "hfdl": False,
        },
        "prohibitions": list(PROHIBITIONS),
    }
    return {
        **unsigned,
        "receipt_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def _build_publication_plan(
    *,
    candidate: RehabilitationCandidate,
    publication_policy_id: str,
    repository: Mapping[str, str],
    code_closure_sha256: str,
    config_closure_sha256: str,
    environment_id: str,
    accepted_root: Path,
    work_root: Path,
    created_at: str,
    synthetic_permit_id: str | None,
) -> dict[str, Any]:
    parse_utc_z(created_at, "rehabilitation publication created_at")
    for name, value in (
        ("publication_policy_id", publication_policy_id),
        ("code_closure_sha256", code_closure_sha256),
        ("config_closure_sha256", config_closure_sha256),
        ("environment_id", environment_id),
    ):
        require_sha256(value, name)
    accepted = Path(accepted_root)
    work = Path(work_root)
    if not accepted.is_absolute() or not work.is_absolute():
        raise ContractError("rehabilitation publication roots must be absolute")
    unsigned = {
        "schema_version": 2,
        "project": PROJECT,
        "mode": (
            "SYNTHETIC_FIXTURE_NOT_GENERATED_EVIDENCE"
            if synthetic_permit_id is not None
            else "PUBLISH_ONE_PIT_UNRESOLVED_LEGACY_DISCOVERY_RELEASE"
        ),
        "publication_policy_id": publication_policy_id,
        "candidate_id": candidate.candidate_id,
        "assessment_id": candidate.assessment_id,
        "publisher_commit": repository["commit"],
        "publisher_tree": repository["tree"],
        "code_closure_sha256": code_closure_sha256,
        "config_closure_sha256": config_closure_sha256,
        "environment_id": environment_id,
        "created_at": created_at,
        "accepted_root": str(accepted),
        "work_root": str(work),
        "dataset": DATASET,
        "source_epoch": SOURCE_EPOCH,
        "row_count": candidate.row_count,
        "page_count": len(candidate.pages),
        "bars_sha256": sha256_bytes(candidate.bars_bytes),
        "source_evidence_manifest_sha256": sha256_bytes(
            candidate.evidence_manifest_bytes
        ),
        "synthetic_permit_id": synthetic_permit_id,
        "authorities": {
            "legacy_discovery_publication": synthetic_permit_id is None,
            "source_activation": False,
            "training_or_research": False,
            "network_or_credentials": False,
            "hfdl": False,
        },
        "publication_count": 1,
        "retry_authorized": False,
        "cleanup_authorized": False,
    }
    publication_plan_id = sha256_bytes(canonical_json_bytes(unsigned))
    receipt = _receipt(
        candidate=candidate,
        publication_plan_id=publication_plan_id,
        publication_policy_id=publication_policy_id,
        created_at=created_at,
        synthetic_permit_id=synthetic_permit_id,
    )
    receipt_bytes = canonical_json_bytes(receipt)
    manifest = _manifest_from_files(
        candidate=candidate,
        receipt_bytes=receipt_bytes,
        created_at=created_at,
        code_hash=code_closure_sha256,
        config_hash=config_closure_sha256,
        environment_hash=environment_id,
    )
    return {
        **unsigned,
        "publication_plan_id": publication_plan_id,
        "prospective_release": {
            "release_id": manifest.release_id,
            "path": str(accepted / DATASET / manifest.release_id),
            "receipt_id": receipt["receipt_id"],
            "file_count": len(manifest.files),
            "files_sha256": sha256_bytes(
                canonical_json_bytes([entry.as_dict() for entry in manifest.files])
            ),
        },
    }


def _production_context(
    *,
    repository_root: Path,
    accepted_root: Path | None,
    work_root: Path | None,
    created_at: str,
) -> tuple[dict[str, Any], RehabilitationCandidate, dict[str, Any]]:
    root = Path(repository_root).resolve(strict=True)
    publication_policy, publication_policy_id = (
        load_rehabilitation_publication_policy(root)
    )
    rehabilitation_policy, rehabilitation_policy_id = (
        load_alpaca_archive_rehabilitation_policy(root)
    )
    hfdl_policy, hfdl_policy_id = load_hfdl_retirement_policy(root)
    if (
        hfdl_policy["state"] != RETIRED_STATE
        or rehabilitation_policy_id
        != publication_policy["rehabilitation_policy_id"]
        or hfdl_policy_id != publication_policy["hfdl_retirement_policy_id"]
    ):
        raise IntegrityError("rehabilitation policy bindings changed")
    archive_root = Path(rehabilitation_policy["legacy_archive_root"])
    assessment = build_alpaca_archive_rehabilitation_plan(
        archive_root,
        repository_root=root,
    )
    if assessment["plan_id"] != publication_policy["assessment_binding"]["plan_id"]:
        raise IntegrityError("rehabilitation assessment identity changed")
    candidate = build_rehabilitation_candidate(
        archive_root,
        expectations=_expectations(rehabilitation_policy),
        metadata_evidence_files=rehabilitation_policy["input_contract"][
            "metadata_evidence_files"
        ],
        assessment_id=assessment["plan_id"],
        rehabilitation_policy_id=rehabilitation_policy_id,
        hfdl_retirement_policy_id=hfdl_policy_id,
        evidence_boundary=rehabilitation_policy["evidence_boundary"],
        expected_inventory=publication_policy["assessment_binding"],
        _validated_inventory=assessment["inventory"],
    )
    outputs = publication_policy["outputs"]
    accepted = Path(accepted_root or root / outputs["accepted_root"]).resolve()
    work = Path(work_root or root / outputs["work_root"]).resolve()
    expected_accepted = (root / "data" / "vault" / "accepted").resolve()
    expected_work = (root / "data" / "w" / "alpaca_archive_rehabilitation").resolve()
    if accepted != expected_accepted or work != expected_work:
        raise ContractError("rehabilitation publication roots differ from policy")
    require_contained_path(accepted, root / "data", must_exist=False)
    require_contained_path(work, root / "data", must_exist=False)
    repository = _repository_binding(root)
    environment_id = validate_environment_lock(root / "config/environment.lock.json")
    plan = _build_publication_plan(
        candidate=candidate,
        publication_policy_id=publication_policy_id,
        repository=repository,
        code_closure_sha256=_closure(root, CODE_CLOSURE_PATHS)[
            "closure_sha256"
        ],
        config_closure_sha256=_closure(root, CONFIG_CLOSURE_PATHS)[
            "closure_sha256"
        ],
        environment_id=environment_id,
        accepted_root=accepted,
        work_root=work,
        created_at=created_at,
        synthetic_permit_id=None,
    )
    return publication_policy, candidate, plan


def build_rehabilitation_publication_plan(
    *,
    repository_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
    created_at: str,
) -> dict[str, Any]:
    """Prepare the exact production publication identity without writing."""

    _policy, _candidate, plan = _production_context(
        repository_root=Path(repository_root or _repo_root()),
        accepted_root=accepted_root,
        work_root=work_root,
        created_at=created_at,
    )
    return plan


def build_synthetic_rehabilitation_publication_plan(
    *,
    candidate: RehabilitationCandidate,
    accepted_root: Path,
    work_root: Path,
    created_at: str,
    permit: SyntheticOnlyPermit,
    publication_policy_id: str,
) -> dict[str, Any]:
    verified = require_synthetic_permit(
        permit,
        scope=SYNTHETIC_PUBLICATION_SCOPE,
    )
    if verified.fixture_id != candidate.candidate_id:
        raise ContractError("synthetic rehabilitation fixture differs from its permit")
    return _build_publication_plan(
        candidate=candidate,
        publication_policy_id=publication_policy_id,
        repository={
            "commit": "0" * 40,
            "tree": "0" * 40,
        },
        code_closure_sha256="0" * 64,
        config_closure_sha256="0" * 64,
        environment_id="0" * 64,
        accepted_root=Path(accepted_root),
        work_root=Path(work_root),
        created_at=created_at,
        synthetic_permit_id=verified.permit_id,
    )


def _validate_plan(
    plan: Mapping[str, Any],
    *,
    candidate: RehabilitationCandidate,
) -> None:
    if (
        plan.get("candidate_id") != candidate.candidate_id
        or plan.get("assessment_id") != candidate.assessment_id
        or plan.get("dataset") != DATASET
        or plan.get("source_epoch") != SOURCE_EPOCH
        or plan.get("row_count") != candidate.row_count
        or plan.get("page_count") != len(candidate.pages)
        or plan.get("publication_count") != 1
        or plan.get("retry_authorized") is not False
        or plan.get("cleanup_authorized") is not False
        or plan.get("authorities")
        != {
            "legacy_discovery_publication": plan.get("synthetic_permit_id") is None,
            "source_activation": False,
            "training_or_research": False,
            "network_or_credentials": False,
            "hfdl": False,
        }
    ):
        raise IntegrityError("rehabilitation publication plan bindings differ")
    unsigned = {
        key: value
        for key, value in plan.items()
        if key not in {"publication_plan_id", "prospective_release"}
    }
    if plan.get("publication_plan_id") != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("rehabilitation publication plan ID differs")
    receipt = _receipt(
        candidate=candidate,
        publication_plan_id=str(plan["publication_plan_id"]),
        publication_policy_id=str(plan["publication_policy_id"]),
        created_at=str(plan["created_at"]),
        synthetic_permit_id=plan.get("synthetic_permit_id"),
    )
    manifest = _manifest_from_files(
        candidate=candidate,
        receipt_bytes=canonical_json_bytes(receipt),
        created_at=str(plan["created_at"]),
        code_hash=str(plan["code_closure_sha256"]),
        config_hash=str(plan["config_closure_sha256"]),
        environment_hash=str(plan["environment_id"]),
    )
    expected = {
        "release_id": manifest.release_id,
        "path": str(Path(str(plan["accepted_root"])) / DATASET / manifest.release_id),
        "receipt_id": receipt["receipt_id"],
        "file_count": len(manifest.files),
        "files_sha256": sha256_bytes(
            canonical_json_bytes([entry.as_dict() for entry in manifest.files])
        ),
    }
    if plan.get("prospective_release") != expected:
        raise IntegrityError("rehabilitation prospective release identity differs")


def _write_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        reject_link(path)
        if (
            not path.is_file()
            or path.stat().st_nlink != 1
            or path.read_bytes() != payload
        ):
            raise IntegrityError(f"rehabilitation staging file differs: {path}")
        return
    atomic_write_new(path, payload)


def _expected_directories(relative_paths: Iterable[str]) -> set[str]:
    directories: set[str] = set()
    for relative in relative_paths:
        parent = Path(relative).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _publish_prevalidated(
    *,
    candidate: RehabilitationCandidate,
    plan: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    synthetic: bool,
) -> RehabilitationPublication:
    _validate_plan(plan, candidate=candidate)
    accepted = Path(accepted_root)
    work = Path(work_root)
    if (
        not accepted.is_absolute()
        or not work.is_absolute()
        or str(accepted) != plan["accepted_root"]
        or str(work) != plan["work_root"]
    ):
        raise ContractError("rehabilitation publication root binding differs")
    receipt = _receipt(
        candidate=candidate,
        publication_plan_id=str(plan["publication_plan_id"]),
        publication_policy_id=str(plan["publication_policy_id"]),
        created_at=str(plan["created_at"]),
        synthetic_permit_id=plan.get("synthetic_permit_id"),
    )
    for page in candidate.pages:
        reject_link(page.source_path)
        if (
            not page.source_path.is_file()
            or page.source_path.stat().st_nlink != 1
            or page.source_path.stat().st_size != page.compressed_size
            or sha256_file(page.source_path) != page.compressed_sha256
        ):
            raise IntegrityError("rehabilitation source page changed before copy")
    stage = work / str(plan["publication_plan_id"]) / "stage"
    relative_paths = [
        BARS_PATH,
        EVIDENCE_PATH,
        RECEIPT_PATH,
        *[page.output_relative for page in candidate.pages],
    ]
    expected_directories = _expected_directories(relative_paths)
    if stage.exists():
        reject_link(stage)
        actual_files = {
            path.relative_to(stage).as_posix()
            for path in stage.rglob("*")
            if path.is_file()
        }
        actual_directories = {
            path.relative_to(stage).as_posix()
            for path in stage.rglob("*")
            if path.is_dir()
        }
        if (
            not actual_files <= set(relative_paths)
            or not actual_directories <= expected_directories
        ):
            raise IntegrityError("rehabilitation stage contains unexpected entries")
    _write_exact(stage / BARS_PATH, candidate.bars_bytes)
    _write_exact(stage / EVIDENCE_PATH, candidate.evidence_manifest_bytes)
    _write_exact(stage / RECEIPT_PATH, canonical_json_bytes(receipt))
    for page in candidate.pages:
        _write_exact(stage / page.output_relative, page.source_path.read_bytes())
        if os.path.samefile(page.source_path, stage / page.output_relative):
            raise IntegrityError("rehabilitation page copy shares source file identity")
    try:
        assert_exact_tree(
            stage,
            set(relative_paths),
            expected_directories,
        )
    except ContractError as exc:
        raise IntegrityError("rehabilitation publication stage differs") from exc
    manifest = _manifest_from_files(
        candidate=candidate,
        receipt_bytes=canonical_json_bytes(receipt),
        created_at=str(plan["created_at"]),
        code_hash=str(plan["code_closure_sha256"]),
        config_hash=str(plan["config_closure_sha256"]),
        environment_hash=str(plan["environment_id"]),
    )
    release_directory = AtomicReleasePublisher(accepted).publish(stage, manifest)
    loaded = verify_rehabilitation_release(
        release_directory,
        accepted_root=accepted,
        expected_plan_id=str(plan["publication_plan_id"]),
        synthetic=synthetic,
    )
    return RehabilitationPublication(
        publication_plan_id=str(plan["publication_plan_id"]),
        release_id=manifest.release_id,
        receipt_id=str(loaded["receipt_id"]),
        release_directory=release_directory,
        work_directory=stage.parent,
    )


def publish_rehabilitation_release(
    *,
    approved_plan_id: str,
    created_at: str,
    repository_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
    owner_confirmation: str,
) -> RehabilitationPublication:
    """Publish once only after a separately approved exact production plan."""

    require_sha256(approved_plan_id, "approved rehabilitation publication plan ID")
    if owner_confirmation != PUBLICATION_CONFIRMATION_VALUE:
        raise PermissionError("rehabilitation publication owner confirmation differs")
    _policy, candidate, plan = _production_context(
        repository_root=Path(repository_root or _repo_root()),
        accepted_root=accepted_root,
        work_root=work_root,
        created_at=created_at,
    )
    if plan["publication_plan_id"] != approved_plan_id:
        raise PermissionError("approved rehabilitation publication plan ID differs")
    return _publish_prevalidated(
        candidate=candidate,
        plan=plan,
        accepted_root=Path(plan["accepted_root"]),
        work_root=Path(plan["work_root"]),
        synthetic=False,
    )


def publish_rehabilitation_fixture(
    *,
    candidate: RehabilitationCandidate,
    plan: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    fixture_root: Path,
    permit: SyntheticOnlyPermit,
) -> RehabilitationPublication:
    verified = require_synthetic_permit(
        permit,
        scope=SYNTHETIC_PUBLICATION_SCOPE,
    )
    fixture = Path(fixture_root).resolve(strict=True)
    require_contained_path(candidate.archive_root, fixture)
    for destination in (Path(accepted_root), Path(work_root)):
        if not destination.is_absolute():
            raise ContractError("synthetic rehabilitation roots must be absolute")
        require_contained_path(destination, fixture, must_exist=False)
    if (
        verified.fixture_id != candidate.candidate_id
        or plan.get("synthetic_permit_id") != verified.permit_id
        or plan.get("mode") != "SYNTHETIC_FIXTURE_NOT_GENERATED_EVIDENCE"
    ):
        raise ContractError("synthetic rehabilitation plan differs from its permit")
    return _publish_prevalidated(
        candidate=candidate,
        plan=plan,
        accepted_root=Path(accepted_root),
        work_root=Path(work_root),
        synthetic=True,
    )


def _validate_receipt(
    receipt: Mapping[str, Any],
    *,
    synthetic: bool,
) -> None:
    expected_fields = {
        "schema_version",
        "project",
        "status",
        "created_at",
        "publication_plan_id",
        "publication_policy_id",
        "candidate_id",
        "assessment_id",
        "rehabilitation_policy_id",
        "hfdl_retirement_policy_id",
        "source_epoch",
        "input_census",
        "outputs",
        "evidence_boundary",
        "synthetic_permit_id",
        "authorities",
        "prohibitions",
        "receipt_id",
    }
    if set(receipt) != expected_fields:
        raise IntegrityError("rehabilitation receipt fields differ")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    require_sha256(receipt["receipt_id"], "rehabilitation receipt_id")
    if receipt["receipt_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("rehabilitation receipt ID differs")
    for name in (
        "publication_plan_id",
        "publication_policy_id",
        "candidate_id",
        "assessment_id",
        "rehabilitation_policy_id",
        "hfdl_retirement_policy_id",
    ):
        require_sha256(receipt[name], f"rehabilitation receipt {name}")
    if (
        receipt["schema_version"] != 2
        or receipt["project"] != PROJECT
        or receipt["status"] != (SYNTHETIC_STATUS if synthetic else PRODUCTION_STATUS)
        or receipt["source_epoch"] != SOURCE_EPOCH
        or receipt["authorities"]
        != {
            "legacy_discovery_publication": not synthetic,
            "active_source": False,
            "eligible_universe": False,
            "features_or_outcomes": False,
            "training_or_evaluation": False,
            "research": False,
            "hfdl": False,
        }
        or receipt["prohibitions"] != list(PROHIBITIONS)
        or (receipt["synthetic_permit_id"] is not None) is not synthetic
    ):
        raise IntegrityError("rehabilitation receipt weakens the caveated boundary")
    parse_utc_z(receipt["created_at"], "rehabilitation receipt created_at")
    if synthetic:
        require_sha256(
            receipt["synthetic_permit_id"],
            "rehabilitation receipt synthetic_permit_id",
        )
    outputs = receipt["outputs"]
    if (
        type(outputs) is not dict
        or outputs.get("bars_path") != BARS_PATH
        or outputs.get("source_evidence_manifest_path") != EVIDENCE_PATH
        or outputs.get("schema_fingerprint") != SCHEMA_FINGERPRINT
        or type(outputs.get("copied_page_count")) is not int
        or outputs["copied_page_count"] < 1
    ):
        raise IntegrityError("rehabilitation receipt outputs differ")
    for name in (
        "bars_sha256",
        "source_evidence_manifest_sha256",
        "copied_page_census_sha256",
    ):
        require_sha256(outputs[name], f"rehabilitation outputs.{name}")


def verify_rehabilitation_release(
    release_directory: Path,
    *,
    accepted_root: Path,
    expected_plan_id: str | None = None,
    synthetic: bool = False,
) -> dict[str, Any]:
    root = Path(release_directory)
    manifest = verify_accepted_release(
        root,
        accepted_root=Path(accepted_root),
    )
    if (
        manifest.dataset != DATASET
        or manifest.source_epoch != SOURCE_EPOCH
        or manifest.role != ROLE
        or manifest.quality_state != QUALITY_STATE
        or manifest.schema_fingerprint != SCHEMA_FINGERPRINT
    ):
        raise IntegrityError("rehabilitation release manifest differs")
    receipt = _json_object(
        root / RECEIPT_PATH,
        label="rehabilitation receipt",
        require_canonical=True,
    )
    _validate_receipt(receipt, synthetic=synthetic)
    if (
        expected_plan_id is not None
        and receipt["publication_plan_id"] != expected_plan_id
    ):
        raise IntegrityError("rehabilitation receipt plan differs")
    evidence = _json_object(
        root / EVIDENCE_PATH,
        label="rehabilitation source evidence manifest",
        require_canonical=True,
    )
    if (
        evidence.get("candidate_id") != receipt["candidate_id"]
        or evidence.get("assessment_id") != receipt["assessment_id"]
        or evidence.get("source_epoch") != SOURCE_EPOCH
        or evidence.get("canonical_parquet_regenerated") is not True
        or evidence.get("derived_legacy_parquet_copied") is not False
        or evidence.get("copied_page_count")
        != receipt["outputs"]["copied_page_count"]
        or sha256_file(root / EVIDENCE_PATH)
        != receipt["outputs"]["source_evidence_manifest_sha256"]
    ):
        raise IntegrityError("rehabilitation source evidence binding differs")
    pages = evidence.get("pages")
    if type(pages) is not list or len(pages) != receipt["outputs"]["copied_page_count"]:
        raise IntegrityError("rehabilitation copied page census differs")
    if sha256_bytes(canonical_json_bytes(pages)) != receipt["outputs"][
        "copied_page_census_sha256"
    ]:
        raise IntegrityError("rehabilitation copied page census hash differs")
    for page in pages:
        if type(page) is not dict:
            raise IntegrityError("rehabilitation page evidence is malformed")
        copied = root / str(page["output_relative"])
        require_contained_path(copied, root)
        if (
            copied.stat().st_nlink != 1
            or copied.stat().st_size != page["compressed_size"]
            or sha256_file(copied) != page["compressed_sha256"]
        ):
            raise IntegrityError("rehabilitation copied page identity differs")
    bars_path = root / BARS_PATH
    if (
        sha256_file(bars_path) != receipt["outputs"]["bars_sha256"]
        or bars_path.stat().st_size != receipt["outputs"]["bars_size"]
    ):
        raise IntegrityError("rehabilitation bars identity differs")
    table = pq.read_table(bars_path)
    if not table.schema.equals(REHABILITATED_ALPACA_SCHEMA):
        raise IntegrityError("rehabilitation Parquet schema differs")
    if table.num_rows != manifest.row_count:
        raise IntegrityError("rehabilitation Parquet row count differs")
    if (
        table["asset_id"].null_count != table.num_rows
        or table["available_at"].null_count != table.num_rows
        or set(pc.unique(table["evidence_class"]).to_pylist()) != {EVIDENCE_CLASS}
        or set(pc.unique(table["quality_state"]).to_pylist()) != {QUALITY_STATE}
        or set(pc.unique(table["historical_proxy"]).to_pylist()) != {True}
        or set(pc.unique(table["point_in_time_safe"]).to_pylist()) != {False}
    ):
        raise IntegrityError("rehabilitation Parquet caveat columns differ")
    expected_files = {
        BARS_PATH,
        EVIDENCE_PATH,
        RECEIPT_PATH,
        *[str(page["output_relative"]) for page in pages],
    }
    if {entry.path for entry in manifest.files} != expected_files:
        raise IntegrityError("rehabilitation release file census differs")
    if (
        manifest.created_at != receipt["created_at"]
        or manifest.event_start != receipt["input_census"]["event_start"]
        or manifest.event_end != receipt["input_census"]["event_end"]
    ):
        raise IntegrityError("rehabilitation release timing differs")
    return receipt
