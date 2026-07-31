from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pyarrow as pa

from ..canonical.parquet import deterministic_parquet_bytes
from ..common import (
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
from ..environment import validate_environment_lock
from ..errors import ContractError, IntegrityError
from ..releases import (
    AtomicReleasePublisher,
    ReleaseFile,
    ReleaseManifest,
    verify_accepted_release,
)
from .alpaca_historical_backfill import (
    _calendar_sessions,
    _retained_snapshot_inventory,
    _validated_plan_id,
    build_historical_backfill_complete_corpus,
    build_historical_backfill_plan,
    load_historical_backfill_policy,
    verify_historical_backfill_unit,
)
from .snapshots import AsReceivedSnapshotStore, LandedSnapshot, NetworkAcquisitionRegistry


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = "config/alpaca_historical_backfill_publication_policy.json"
MODE = "ALPACA_HISTORICAL_BACKFILL_PUBLICATION_PLAN_ONLY"
DATASET = "alpaca_historical_daily_bars"
SOURCE_EPOCH = "alpaca_sip_current_identity_seeded_20160104_20260710_v1"
ROLE = "legacy_discovery_only"
QUALITY_STATE = "LEGACY_CAVEATED"
INPUT_QUALITY_STATE = "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED"
EVIDENCE_MANIFEST_PATH = "source_evidence_manifest.json"
SNAPSHOT_EVIDENCE_PREFIX = "source_snapshots"
SNAPSHOT_EVIDENCE_DIRECTORY_LENGTH = 20
PUBLICATION_CONFIRMATION_TOKEN = "ALPACA_HISTORICAL_BACKFILL_PUBLICATION_APPROVED"
PUBLICATION_CONFIRMATION_VALUE = "YES"

HISTORICAL_BACKFILL_SCHEMA = pa.schema(
    [
        ("provider_symbol", pa.string()),
        ("asset_id", pa.string()),
        ("security_type", pa.string()),
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
        ("retrieved_at", pa.timestamp("us", tz="UTC")),
        ("source_snapshot_id", pa.string()),
        ("request_plan_id", pa.string()),
        ("source_epoch", pa.string()),
        ("evidence_class", pa.string()),
        ("input_quality_state", pa.string()),
        ("historical_membership_proven", pa.bool_()),
        ("point_in_time_safe", pa.bool_()),
    ]
)
SCHEMA_FINGERPRINT = sha256_bytes(canonical_json_bytes(str(HISTORICAL_BACKFILL_SCHEMA)))

CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/canonical/alpaca.py",
    "src/us_stocks_swing_model_v2/canonical/parquet.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_historical_backfill.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_historical_backfill_publication.py",
    "src/us_stocks_swing_model_v2/cli/plan_alpaca_historical_backfill_publication.py",
    "src/us_stocks_swing_model_v2/cli/publish_alpaca_historical_backfill.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    "config/alpaca_historical_backfill_policy.json",
    POLICY_PATH,
    "config/alpaca_historical_backfill_network_registry.json",
    "config/environment.lock.json",
    "config/sources.json",
)


@dataclass(frozen=True)
class HistoricalBackfillReleaseBuild:
    complete_corpus: Mapping[str, object]
    manifest: ReleaseManifest
    generated_files: tuple[tuple[str, bytes], ...]
    copied_files: tuple[tuple[str, Path], ...]
    shard_census: tuple[dict[str, object], ...]
    evidence_manifest_id: str


@dataclass(frozen=True)
class HistoricalBackfillPublication:
    publication_plan_id: str
    release_id: str
    release_directory: Path
    work_directory: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(value) is not dict:
        raise IntegrityError(f"{label} must be one object")
    return value


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


def load_historical_backfill_publication_policy(
    repo_root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    policy = _json_object(root / POLICY_PATH, label="backfill publication policy")
    policy_id = sha256_bytes(canonical_json_bytes(policy))
    release_contract = {
        "dataset": DATASET,
        "source_epoch": SOURCE_EPOCH,
        "role": ROLE,
        "quality_state": QUALITY_STATE,
        "input_quality_state": INPUT_QUALITY_STATE,
        "canonical_shard": "calendar_year",
        "expected_shard_count": 11,
        "bars_prefix": "bars",
        "source_evidence_manifest_path": EVIDENCE_MANIFEST_PATH,
        "snapshot_evidence_prefix": SNAPSHOT_EVIDENCE_PREFIX,
        "copy_exact_snapshot_evidence": True,
        "regenerate_canonical_parquet": True,
        "active_source_eligible": False,
        "training_authorized": False,
        "research_authorized": False,
    }
    if (
        policy.get("schema_version") != 1
        or policy.get("project") != PROJECT
        or policy.get("mode") != MODE
        or policy.get("release_contract") != release_contract
    ):
        raise ContractError("backfill publication policy contract differs")
    implementation = policy.get("implementation")
    if implementation != {
        "plan_only": True,
        "release_builder_implemented": True,
        "publication_execution_implemented": True,
        "release_id_deferred_until_deterministic_builder": False,
    }:
        raise ContractError("backfill publication implementation state differs")
    authorities = policy.get("authorities")
    if not isinstance(authorities, dict) or any(authorities.values()):
        raise ContractError("backfill publication policy grants authority")
    backfill_policy, backfill_policy_id = load_historical_backfill_policy(root)
    if policy.get("backfill_policy_id") != backfill_policy_id:
        raise IntegrityError("backfill publication policy binding differs")
    if backfill_policy.get("quality_state") != INPUT_QUALITY_STATE:
        raise IntegrityError("backfill publication quality binding differs")
    return policy, policy_id


def _validated_complete_corpus(
    corpus: Mapping[str, object],
    *,
    policy: Mapping[str, Any],
) -> str:
    corpus_id = require_sha256(corpus.get("complete_corpus_id"), "complete corpus ID")
    unsigned = {key: value for key, value in corpus.items() if key != "complete_corpus_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != corpus_id:
        raise IntegrityError("historical backfill complete-corpus ID differs")
    expected = policy["completeness_contract"]
    if (
        corpus.get("plan_type")
        != "ALPACA_SIP_HISTORICAL_BACKFILL_COMPLETE_CORPUS"
        or corpus.get("group_count") != expected["expected_group_count"]
        or corpus.get("unit_count") != expected["expected_unit_count"]
        or not isinstance(corpus.get("page_count"), int)
        or corpus["page_count"] <= 0
        or not isinstance(corpus.get("raw_bytes"), int)
        or corpus["raw_bytes"] <= 0
        or corpus.get("evidence_boundary", {}).get("quality_state")
        != INPUT_QUALITY_STATE
        or corpus.get("evidence_boundary", {}).get("survivorship_safe") is not False
        or any(corpus.get("authorities", {}).values())
    ):
        raise IntegrityError("historical backfill complete-corpus contract differs")
    return corpus_id


def build_historical_backfill_release(
    *,
    backfill_plan: Mapping[str, object],
    complete_corpus: Mapping[str, object],
    snapshot_inventory: Sequence[LandedSnapshot],
    calendar_sessions: Sequence[date],
    registry: NetworkAcquisitionRegistry,
    policy: Mapping[str, Any],
    publication_policy_id: str,
    created_at: str,
    code_hash: str,
    config_hash: str,
    environment_hash: str,
    synthetic: bool,
) -> HistoricalBackfillReleaseBuild:
    """Build exact release bytes and identity without writing or publishing."""

    plan_id = _validated_plan_id(backfill_plan)
    corpus_id = _validated_complete_corpus(complete_corpus, policy=policy)
    if complete_corpus.get("backfill_plan_id") != plan_id:
        raise IntegrityError("historical backfill release plan binding differs")
    parse_utc_z(created_at, "backfill publication created_at")
    for label, value in (
        ("publication_policy_id", publication_policy_id),
        ("code_hash", code_hash),
        ("config_hash", config_hash),
        ("environment_hash", environment_hash),
    ):
        require_sha256(value, label)

    inventory_by_id = {
        snapshot.snapshot_id: snapshot for snapshot in snapshot_inventory
    }
    if len(inventory_by_id) != len(snapshot_inventory):
        raise IntegrityError("historical backfill release inventory is ambiguous")
    page_evidence = complete_corpus.get("page_evidence")
    units = backfill_plan.get("request_units")
    if not isinstance(page_evidence, list) or not isinstance(units, list):
        raise IntegrityError("historical backfill release input census differs")
    evidence_by_unit: dict[int, list[Mapping[str, object]]] = {}
    for entry in page_evidence:
        if not isinstance(entry, Mapping) or type(entry.get("unit_index")) is not int:
            raise IntegrityError("historical backfill page evidence differs")
        evidence_by_unit.setdefault(int(entry["unit_index"]), []).append(entry)
    if sorted(evidence_by_unit) != [int(unit["unit_index"]) for unit in units]:
        raise IntegrityError("historical backfill release unit evidence differs")

    years = sorted({int(unit["window"]["year"]) for unit in units})
    expected_shards = policy["release_contract"]["expected_shard_count"]
    expected_windows = policy["completeness_contract"]["expected_window_count"]
    if len(years) != expected_shards or len(years) != expected_windows:
        raise IntegrityError("historical backfill release shard census differs")

    generated_files: list[tuple[str, bytes]] = []
    shard_census: list[dict[str, object]] = []
    total_rows = 0
    first_session: date | None = None
    last_session: date | None = None
    for year in years:
        tables: list[pa.Table] = []
        observed_symbols: set[str] = set()
        for unit in [value for value in units if int(value["window"]["year"]) == year]:
            symbols = unit.get("symbols")
            if not isinstance(symbols, list) or observed_symbols.intersection(symbols):
                raise IntegrityError("historical backfill release symbol batches overlap")
            observed_symbols.update(symbols)
            entries = sorted(
                evidence_by_unit[int(unit["unit_index"])],
                key=lambda value: int(value["page_index"]),
            )
            if [int(entry["page_index"]) for entry in entries] != list(
                range(1, len(entries) + 1)
            ):
                raise IntegrityError("historical backfill release page order differs")
            pages: list[LandedSnapshot] = []
            for entry in entries:
                snapshot = inventory_by_id.get(str(entry.get("snapshot_id")))
                if (
                    snapshot is None
                    or snapshot.raw_sha256 != entry.get("raw_sha256")
                    or snapshot.raw_path.stat().st_size != entry.get("raw_bytes")
                    or sha256_file(snapshot.root / "headers.json")
                    != entry.get("headers_sha256")
                    or sha256_file(snapshot.root / "receipt.json")
                    != entry.get("receipt_sha256")
                ):
                    raise IntegrityError(
                        "historical backfill release snapshot evidence differs"
                    )
                pages.append(snapshot)
            rows: list[dict[str, object]] = []
            verify_historical_backfill_unit(
                unit,
                pages,
                calendar_sessions=calendar_sessions,
                registry=registry,
                synthetic=synthetic,
                _canonical_row_sink=rows.append,
            )
            for row in rows:
                row.update(
                    {
                        "source_epoch": SOURCE_EPOCH if not synthetic else "SYNTHETIC_ONLY",
                        "evidence_class": (
                            "LEGACY_DISCOVERY"
                            if not synthetic
                            else "SYNTHETIC_MECHANICAL"
                        ),
                        "input_quality_state": INPUT_QUALITY_STATE,
                        "historical_membership_proven": False,
                        "point_in_time_safe": False,
                    }
                )
            if rows:
                tables.append(pa.Table.from_pylist(rows, schema=HISTORICAL_BACKFILL_SCHEMA))
        if not tables:
            raise IntegrityError("historical backfill release shard is empty")
        table = pa.concat_tables(tables)
        shard_bytes = deterministic_parquet_bytes(
            table,
            schema=HISTORICAL_BACKFILL_SCHEMA,
            sort_keys=("provider_symbol", "session"),
        )
        sessions = table.column("session").to_pylist()
        observed_first = min(sessions)
        observed_last = max(sessions)
        first_session = (
            observed_first if first_session is None else min(first_session, observed_first)
        )
        last_session = (
            observed_last if last_session is None else max(last_session, observed_last)
        )
        relative = f"bars/year={year}.parquet"
        generated_files.append((relative, shard_bytes))
        shard_census.append(
            {
                "year": year,
                "path": relative,
                "rows": table.num_rows,
                "bytes": len(shard_bytes),
                "sha256": sha256_bytes(shard_bytes),
                "event_start": observed_first.isoformat(),
                "event_end": observed_last.isoformat(),
            }
        )
        total_rows += table.num_rows

    copied_files: list[tuple[str, Path]] = []
    copied_entries: list[ReleaseFile] = []
    for entry in page_evidence:
        snapshot = inventory_by_id[str(entry["snapshot_id"])]
        for filename in ("headers.json", "raw.bin", "receipt.json"):
            source = snapshot.root / filename
            # The immutable evidence manifest retains the full snapshot ID.
            # A checked 20-hex directory prefix keeps the accepted release
            # readable on Windows' legacy path limit.
            relative = (
                f"{SNAPSHOT_EVIDENCE_PREFIX}/"
                f"{snapshot.snapshot_id[:SNAPSHOT_EVIDENCE_DIRECTORY_LENGTH]}/{filename}"
            )
            copied_files.append((relative, source))
            copied_entries.append(
                ReleaseFile(relative, source.stat().st_size, sha256_file(source))
            )
    if len({path for path, _source in copied_files}) != len(copied_files):
        raise IntegrityError("historical backfill copied evidence is duplicated")

    evidence_unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "backfill_plan_id": plan_id,
        "complete_corpus_id": corpus_id,
        "publication_policy_id": publication_policy_id,
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "input_quality_state": INPUT_QUALITY_STATE,
        "historical_membership_proven": False,
        "survivorship_safe": False,
        "point_in_time_safe": False,
        "page_evidence_census_sha256": complete_corpus[
            "page_evidence_census_sha256"
        ],
        "page_evidence": page_evidence,
        "shards": shard_census,
        "exact_snapshot_evidence_preserved": True,
        "authorities": dict(policy["authorities"]),
    }
    evidence_manifest_id = sha256_bytes(canonical_json_bytes(evidence_unsigned))
    evidence_bytes = canonical_json_bytes(
        {**evidence_unsigned, "evidence_manifest_id": evidence_manifest_id}
    )
    generated_files.append((EVIDENCE_MANIFEST_PATH, evidence_bytes))
    generated_entries = [
        ReleaseFile(path, len(payload), sha256_bytes(payload))
        for path, payload in generated_files
    ]
    files = tuple(sorted((*generated_entries, *copied_entries), key=lambda item: item.path))
    if first_session is None or last_session is None:
        raise IntegrityError("historical backfill release event bounds are absent")
    dataset = DATASET if not synthetic else f"{DATASET}_fixture"
    source_epoch = SOURCE_EPOCH if not synthetic else "SYNTHETIC_ONLY"
    role = ROLE if not synthetic else "qualification_evidence_only"
    quality_state = QUALITY_STATE if not synthetic else "QUALIFICATION_EVIDENCE"
    upstream_release_ids = (
        sorted(
            {
                str(backfill_plan["identity_release"]["release_id"]),
                str(backfill_plan["rehabilitated_release"]["release_id"]),
                str(backfill_plan["calendar_release"]["release_id"]),
            }
        )
        if not synthetic
        else []
    )
    manifest_unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "dataset": dataset,
        "source_epoch": source_epoch,
        "role": role,
        "quality_state": quality_state,
        "created_at": created_at,
        "row_count": total_rows,
        "event_start": first_session.isoformat(),
        "event_end": last_session.isoformat(),
        "upstream_release_ids": upstream_release_ids,
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "environment_hash": environment_hash,
        "files": [entry.as_dict() for entry in files],
    }
    manifest = ReleaseManifest(
        **{
            **manifest_unsigned,
            "upstream_release_ids": tuple(upstream_release_ids),
            "files": files,
            "release_id": sha256_bytes(canonical_json_bytes(manifest_unsigned)),
        }
    )
    manifest.validate()
    return HistoricalBackfillReleaseBuild(
        complete_corpus=dict(complete_corpus),
        manifest=manifest,
        generated_files=tuple(sorted(generated_files)),
        copied_files=tuple(sorted(copied_files)),
        shard_census=tuple(shard_census),
        evidence_manifest_id=evidence_manifest_id,
    )


def build_historical_backfill_publication_plan_from_corpus(
    *,
    release_build: HistoricalBackfillReleaseBuild,
    policy: Mapping[str, Any],
    publication_policy_id: str,
    accepted_root: Path,
    work_root: Path,
    created_at: str,
    code_closure_sha256: str,
    config_closure_sha256: str,
    environment_id: str,
) -> dict[str, object]:
    complete_corpus = release_build.complete_corpus
    corpus_id = _validated_complete_corpus(complete_corpus, policy=policy)
    release_build.manifest.validate()
    parse_utc_z(created_at, "backfill publication created_at")
    for label, value in (
        ("publication_policy_id", publication_policy_id),
        ("code_closure_sha256", code_closure_sha256),
        ("config_closure_sha256", config_closure_sha256),
        ("environment_id", environment_id),
    ):
        require_sha256(value, label)
    accepted = Path(accepted_root)
    work = Path(work_root)
    if not accepted.is_absolute() or not work.is_absolute():
        raise ContractError("backfill publication roots must be absolute")
    release = policy["release_contract"]
    unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "mode": MODE,
        "publication_policy_id": publication_policy_id,
        "backfill_plan_id": complete_corpus["backfill_plan_id"],
        "complete_corpus_id": corpus_id,
        "repository": dict(complete_corpus["repository"]),
        "code_closure_sha256": code_closure_sha256,
        "config_closure_sha256": config_closure_sha256,
        "environment_id": environment_id,
        "created_at": created_at,
        "accepted_root": str(accepted),
        "work_root": str(work),
        "input_census": {
            "group_count": complete_corpus["group_count"],
            "unit_count": complete_corpus["unit_count"],
            "page_count": complete_corpus["page_count"],
            "raw_bytes": complete_corpus["raw_bytes"],
            "group_continuation_ids_sha256": complete_corpus[
                "group_continuation_ids_sha256"
            ],
            "unit_assessment_ids_sha256": complete_corpus[
                "unit_assessment_ids_sha256"
            ],
            "selected_snapshot_ids_sha256": complete_corpus[
                "selected_snapshot_ids_sha256"
            ],
            "page_evidence_census_sha256": complete_corpus[
                "page_evidence_census_sha256"
            ],
        },
        "release_contract": dict(release),
        "prospective_release": {
            "dataset": release_build.manifest.dataset,
            "release_id": release_build.manifest.release_id,
            "path": str(
                accepted
                / release_build.manifest.dataset
                / release_build.manifest.release_id
            ),
            "manifest_sha256": sha256_bytes(
                canonical_json_bytes(release_build.manifest.as_dict())
            ),
            "row_count": release_build.manifest.row_count,
            "event_start": release_build.manifest.event_start,
            "event_end": release_build.manifest.event_end,
            "schema_fingerprint": release_build.manifest.schema_fingerprint,
            "shard_count": len(release_build.shard_census),
            "generated_file_count": len(release_build.generated_files),
            "copied_evidence_file_count": len(release_build.copied_files),
            "evidence_manifest_id": release_build.evidence_manifest_id,
        },
        "implementation": dict(policy["implementation"]),
        "authorities": dict(policy["authorities"]),
        "stop_conditions": list(policy["stop_conditions"]),
    }
    return {
        **unsigned,
        "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_historical_backfill_publication_plan(
    *,
    repository_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
    created_at: str,
) -> dict[str, object]:
    """Revalidate and build the exact real release identity without writing."""

    root = (repository_root or _repo_root()).resolve(strict=True)
    policy, policy_id = load_historical_backfill_publication_policy(root)
    backfill_policy, _ = load_historical_backfill_policy(root)
    backfill_plan = build_historical_backfill_plan(repo_root=root)
    registry = NetworkAcquisitionRegistry.load(
        root / backfill_policy["network_registry"],
        allowed_root=root,
    )
    accepted_input_root = (root / "data/vault/accepted").resolve(strict=True)
    snapshot_store = AsReceivedSnapshotStore(
        (root / backfill_policy["outputs"]["snapshot_store"]).resolve(strict=True),
        allowed_root=(root / "data").resolve(strict=True),
        acquisition_registry=registry,
    )
    calendar_sessions = _calendar_sessions(
        root,
        backfill_policy,
        accepted_input_root,
    )
    inventory = _retained_snapshot_inventory(snapshot_store)
    complete = build_historical_backfill_complete_corpus(
        backfill_plan=backfill_plan,
        snapshot_store=snapshot_store,
        calendar_sessions=calendar_sessions,
        registry=registry,
        synthetic=False,
        _inventory=inventory,
    )
    expected_accepted = (root / policy["outputs"]["accepted_root"]).resolve()
    expected_work = (root / policy["outputs"]["work_root"]).resolve()
    accepted = Path(accepted_root or expected_accepted).resolve()
    work = Path(work_root or expected_work).resolve()
    if accepted != expected_accepted or work != expected_work:
        raise ContractError("backfill publication roots differ from policy")
    require_contained_path(accepted, root / "data", must_exist=False)
    require_contained_path(work, root / "data", must_exist=False)
    code_hash = _closure(root, CODE_CLOSURE_PATHS)["closure_sha256"]
    config_hash = _closure(root, CONFIG_CLOSURE_PATHS)["closure_sha256"]
    environment_id = validate_environment_lock(root / "config/environment.lock.json")
    release_build = build_historical_backfill_release(
        backfill_plan=backfill_plan,
        complete_corpus=complete,
        snapshot_inventory=inventory,
        calendar_sessions=calendar_sessions,
        registry=registry,
        policy=policy,
        publication_policy_id=policy_id,
        created_at=created_at,
        code_hash=str(code_hash),
        config_hash=str(config_hash),
        environment_hash=environment_id,
        synthetic=False,
    )
    return build_historical_backfill_publication_plan_from_corpus(
        release_build=release_build,
        policy=policy,
        publication_policy_id=policy_id,
        accepted_root=accepted,
        work_root=work,
        created_at=created_at,
        code_closure_sha256=str(code_hash),
        config_closure_sha256=str(config_hash),
        environment_id=environment_id,
    )


def _validate_publication_plan_for_build(
    plan: Mapping[str, object],
    *,
    release_build: HistoricalBackfillReleaseBuild,
) -> None:
    publication_plan_summary(plan)
    prospective = plan.get("prospective_release")
    if not isinstance(prospective, Mapping) or prospective.get("release_id") != release_build.manifest.release_id:
        raise IntegrityError("historical backfill publication release binding differs")
    if prospective.get("manifest_sha256") != sha256_bytes(
        canonical_json_bytes(release_build.manifest.as_dict())
    ):
        raise IntegrityError("historical backfill publication manifest binding differs")
    if plan.get("implementation", {}).get("publication_execution_implemented") is not True:
        raise ContractError("historical backfill publication execution is unavailable")


def _expected_directories(relative_paths: Iterable[str]) -> set[str]:
    directories: set[str] = set()
    for relative in relative_paths:
        parent = Path(relative).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _write_exact_new(path: Path, payload: bytes) -> None:
    if path.exists():
        raise IntegrityError(f"historical backfill publication stage already exists: {path}")
    atomic_write_new(path, payload)


def _publish_release_build(
    *,
    release_build: HistoricalBackfillReleaseBuild,
    plan: Mapping[str, object],
    accepted_root: Path,
    work_root: Path,
) -> HistoricalBackfillPublication:
    _validate_publication_plan_for_build(plan, release_build=release_build)
    accepted = Path(accepted_root)
    work = Path(work_root)
    if (
        not accepted.is_absolute()
        or not work.is_absolute()
        or str(accepted) != plan.get("accepted_root")
        or str(work) != plan.get("work_root")
    ):
        raise ContractError("historical backfill publication root binding differs")
    work.mkdir(parents=True, exist_ok=True)
    require_contained_path(work, work)
    # Keep the work-directory component short enough for Windows paths while
    # retaining the full plan ID in the immutable manifest and result.
    stage = work / str(plan["publication_plan_id"])[:20] / "stage"
    require_contained_path(stage, work, must_exist=False)
    if stage.exists():
        raise IntegrityError("historical backfill publication stage already exists")
    stage.mkdir(parents=True)
    require_contained_path(stage, work)

    generated = dict(release_build.generated_files)
    copied = dict(release_build.copied_files)
    expected_paths = {entry.path for entry in release_build.manifest.files}
    if set(generated) | set(copied) != expected_paths or set(generated) & set(copied):
        raise IntegrityError("historical backfill publication payload census differs")
    for entry in release_build.manifest.files:
        target = stage.joinpath(*Path(entry.path).parts)
        require_contained_path(target, stage, must_exist=False)
        if entry.path in generated:
            payload = generated[entry.path]
        else:
            source = copied[entry.path]
            require_contained_path(source, source)
            reject_link(source)
            if not source.is_file() or source.stat().st_nlink != 1:
                raise IntegrityError("historical backfill copied evidence is not a plain file")
            payload = source.read_bytes()
        if len(payload) != entry.size or sha256_bytes(payload) != entry.sha256:
            raise IntegrityError(f"historical backfill publication payload differs: {entry.path}")
        _write_exact_new(target, payload)
    try:
        assert_exact_tree(
            stage,
            expected_paths,
            _expected_directories(expected_paths),
        )
    except ContractError as exc:
        raise IntegrityError("historical backfill publication stage differs") from exc
    release_directory = AtomicReleasePublisher(accepted).publish(
        stage,
        release_build.manifest,
    )
    verify_accepted_release(
        release_directory,
        accepted_root=accepted,
        expected=release_build.manifest,
    )
    return HistoricalBackfillPublication(
        publication_plan_id=str(plan["publication_plan_id"]),
        release_id=release_build.manifest.release_id,
        release_directory=release_directory,
        work_directory=stage.parent,
    )


def publish_historical_backfill_release(
    *,
    approved_plan_id: str,
    created_at: str,
    repository_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
    owner_confirmation: str,
) -> HistoricalBackfillPublication:
    """Publish one exact, separately approved legacy-discovery release."""

    require_sha256(approved_plan_id, "approved historical backfill publication plan ID")
    if (
        owner_confirmation != PUBLICATION_CONFIRMATION_VALUE
        or os.environ.get(PUBLICATION_CONFIRMATION_TOKEN)
        != PUBLICATION_CONFIRMATION_VALUE
    ):
        raise PermissionError("historical backfill publication confirmation is absent")
    root = (repository_root or _repo_root()).resolve(strict=True)
    policy, policy_id = load_historical_backfill_publication_policy(root)
    backfill_policy, _ = load_historical_backfill_policy(root)
    backfill_plan = build_historical_backfill_plan(repo_root=root)
    registry = NetworkAcquisitionRegistry.load(
        root / backfill_policy["network_registry"], allowed_root=root
    )
    snapshot_store = AsReceivedSnapshotStore(
        (root / backfill_policy["outputs"]["snapshot_store"]).resolve(strict=True),
        allowed_root=(root / "data").resolve(strict=True),
        acquisition_registry=registry,
    )
    accepted_input_root = (root / "data/vault/accepted").resolve(strict=True)
    calendar_sessions = _calendar_sessions(root, backfill_policy, accepted_input_root)
    inventory = _retained_snapshot_inventory(snapshot_store)
    complete = build_historical_backfill_complete_corpus(
        backfill_plan=backfill_plan,
        snapshot_store=snapshot_store,
        calendar_sessions=calendar_sessions,
        registry=registry,
        synthetic=False,
        _inventory=inventory,
    )
    expected_accepted = (root / policy["outputs"]["accepted_root"]).resolve()
    expected_work = (root / policy["outputs"]["work_root"]).resolve()
    accepted = Path(accepted_root or expected_accepted).resolve()
    work = Path(work_root or expected_work).resolve()
    if accepted != expected_accepted or work != expected_work:
        raise ContractError("historical backfill publication roots differ from policy")
    code_hash = _closure(root, CODE_CLOSURE_PATHS)["closure_sha256"]
    config_hash = _closure(root, CONFIG_CLOSURE_PATHS)["closure_sha256"]
    environment_id = validate_environment_lock(root / "config/environment.lock.json")
    release_build = build_historical_backfill_release(
        backfill_plan=backfill_plan,
        complete_corpus=complete,
        snapshot_inventory=inventory,
        calendar_sessions=calendar_sessions,
        registry=registry,
        policy=policy,
        publication_policy_id=policy_id,
        created_at=created_at,
        code_hash=str(code_hash),
        config_hash=str(config_hash),
        environment_hash=environment_id,
        synthetic=False,
    )
    plan = build_historical_backfill_publication_plan_from_corpus(
        release_build=release_build,
        policy=policy,
        publication_policy_id=policy_id,
        accepted_root=accepted,
        work_root=work,
        created_at=created_at,
        code_closure_sha256=str(code_hash),
        config_closure_sha256=str(config_hash),
        environment_id=environment_id,
    )
    if plan["publication_plan_id"] != approved_plan_id:
        raise PermissionError("approved historical backfill publication plan ID differs")
    return _publish_release_build(
        release_build=release_build,
        plan=plan,
        accepted_root=accepted,
        work_root=work,
    )


def publish_historical_backfill_fixture(
    *,
    release_build: HistoricalBackfillReleaseBuild,
    plan: Mapping[str, object],
    accepted_root: Path,
    work_root: Path,
) -> HistoricalBackfillPublication:
    """Synthetic-only publication helper for contract tests."""

    return _publish_release_build(
        release_build=release_build,
        plan=plan,
        accepted_root=accepted_root,
        work_root=work_root,
    )


def publication_plan_summary(plan: Mapping[str, object]) -> dict[str, object]:
    plan_id = require_sha256(plan.get("publication_plan_id"), "publication plan ID")
    unsigned = {key: value for key, value in plan.items() if key != "publication_plan_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != plan_id:
        raise IntegrityError("historical backfill publication plan ID differs")
    return {
        key: plan[key]
        for key in (
            "publication_plan_id",
            "backfill_plan_id",
            "complete_corpus_id",
            "repository",
            "created_at",
            "input_census",
            "release_contract",
            "prospective_release",
            "implementation",
            "authorities",
            "stop_conditions",
        )
    }
