from __future__ import annotations

import importlib.metadata
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import exchange_calendars
import pyarrow as pa
import pyarrow.parquet as pq

from .calendar import PinnedSessionCalendar
from .canonical.parquet import deterministic_table, write_deterministic_parquet
from .common import atomic_write, canonical_json_bytes, parse_utc_z, reject_link, sha256_bytes, sha256_file
from .errors import ContractError, IntegrityError
from .releases import AtomicReleasePublisher, build_manifest, verify_accepted_release


EXCHANGE_CALENDARS_VERSION = "4.13.2"
CALENDAR_NAME = "XNYS"
SOURCE_EPOCH = "xnys_exchange_calendars_4_13_2"
CALENDAR_SCHEMA = pa.schema(
    [
        ("session", pa.date32()),
        ("open_at", pa.timestamp("us", tz="UTC")),
        ("close_at", pa.timestamp("us", tz="UTC")),
        ("early_close", pa.bool_()),
        ("calendar_name", pa.string()),
        ("calendar_package", pa.string()),
        ("calendar_version", pa.string()),
    ]
)


@dataclass(frozen=True)
class LoadedExchangeCalendar:
    calendar: PinnedSessionCalendar
    schedule: pa.Table
    provenance: dict[str, object]


def publish_xnys_calendar_release(
    *,
    staging_root: Path,
    release_root: Path,
    start: date,
    end: date,
    created_at: str,
    code_hash: str,
    config_hash: str,
    environment_hash: str,
) -> Path:
    """Generate and publish a version-pinned XNYS schedule without consulting bars."""
    parse_utc_z(created_at, "created_at")
    if start > end:
        raise ContractError("calendar release start cannot follow end")
    installed = importlib.metadata.version("exchange-calendars")
    if installed != EXCHANGE_CALENDARS_VERSION:
        raise ContractError("exchange-calendars runtime differs from the pinned calendar contract")
    stage = Path(staging_root)
    releases = Path(release_root)
    if stage.exists() and any(stage.iterdir()):
        raise ContractError("calendar staging root must be new or empty")
    stage.mkdir(parents=True, exist_ok=True)
    reject_link(stage)
    # Pin the generation range explicitly. The library's default calendar
    # bounds are relative to the system date and would make a future rebuild of
    # the same requested release silently lose old or future sessions.
    calendar = exchange_calendars.get_calendar(
        CALENDAR_NAME,
        start=start.isoformat(),
        end=end.isoformat(),
    )
    schedule = calendar.schedule.loc[start.isoformat() : end.isoformat()]
    if schedule.empty:
        raise ContractError("XNYS range contains no sessions")
    rows: list[dict[str, object]] = []
    for label, values in schedule.iterrows():
        open_at = values["open"].to_pydatetime()
        close_at = values["close"].to_pydatetime()
        duration_minutes = int((close_at - open_at).total_seconds() // 60)
        rows.append(
            {
                "session": label.date(),
                "open_at": open_at,
                "close_at": close_at,
                "early_close": duration_minutes < 390,
                "calendar_name": CALENDAR_NAME,
                "calendar_package": "exchange-calendars",
                "calendar_version": installed,
            }
        )
    table = deterministic_table(
        pa.Table.from_pylist(rows, schema=CALENDAR_SCHEMA),
        CALENDAR_SCHEMA,
        ("session",),
    )
    schedule_path = write_deterministic_parquet(
        table,
        stage / "sessions.parquet",
        schema=CALENDAR_SCHEMA,
        sort_keys=("session",),
    )
    provenance = {
        "schema_version": 1,
        "calendar_name": CALENDAR_NAME,
        "calendar_package": "exchange-calendars",
        "calendar_version": installed,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "first_session": rows[0]["session"].isoformat(),
        "last_session": rows[-1]["session"].isoformat(),
        "session_count": len(rows),
        "early_close_count": sum(bool(row["early_close"]) for row in rows),
        "created_at": created_at,
        "source_contract": "versioned_XNYS_rules_never_infer_sessions_from_observed_bars",
        "revision_policy": "calendar_package_or_range_or_config_change_requires_new_release",
    }
    atomic_write(stage / "provenance.json", canonical_json_bytes(provenance))
    schema_fingerprint = sha256_bytes(canonical_json_bytes(str(CALENDAR_SCHEMA)))
    manifest = build_manifest(
        stage,
        (schedule_path.name, "provenance.json"),
        project="US_stocks_swing_model_v2",
        dataset="xnys_sessions",
        source_epoch=SOURCE_EPOCH,
        role="derived_causal",
        quality_state="PASS",
        created_at=created_at,
        row_count=len(rows),
        event_start=rows[0]["session"].isoformat(),
        event_end=rows[-1]["session"].isoformat(),
        schema_fingerprint=schema_fingerprint,
        code_hash=code_hash,
        config_hash=config_hash,
        environment_hash=environment_hash,
    )
    return AtomicReleasePublisher(releases).publish(stage, manifest)


def load_xnys_calendar_release(
    release_directory: Path,
    *,
    accepted_release_root: Path,
) -> LoadedExchangeCalendar:
    manifest = verify_accepted_release(
        Path(release_directory),
        accepted_root=Path(accepted_release_root),
    )
    if (
        manifest.project != "US_stocks_swing_model_v2"
        or manifest.dataset != "xnys_sessions"
        or manifest.source_epoch != SOURCE_EPOCH
        or manifest.role != "derived_causal"
        or manifest.quality_state != "PASS"
    ):
        raise ContractError("release is not a trust-eligible pinned XNYS calendar")
    try:
        provenance = json.loads((Path(release_directory) / "provenance.json").read_text(encoding="utf-8"))
        table = pq.read_table(Path(release_directory) / "sessions.parquet")
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("XNYS calendar release payload is unreadable") from exc
    if table.schema.remove_metadata() != CALENDAR_SCHEMA or table.num_rows != manifest.row_count:
        raise IntegrityError("XNYS calendar schedule schema/count differs from its release")
    if set(provenance) != {
        "schema_version",
        "calendar_name",
        "calendar_package",
        "calendar_version",
        "requested_start",
        "requested_end",
        "first_session",
        "last_session",
        "session_count",
        "early_close_count",
        "created_at",
        "source_contract",
        "revision_policy",
    }:
        raise IntegrityError("XNYS calendar provenance fields differ from the exact contract")
    if (
        provenance["calendar_version"] != EXCHANGE_CALENDARS_VERSION
        or provenance["calendar_name"] != CALENDAR_NAME
        or provenance["session_count"] != table.num_rows
        or provenance["source_contract"]
        != "versioned_XNYS_rules_never_infer_sessions_from_observed_bars"
    ):
        raise IntegrityError("XNYS calendar provenance differs from its schedule contract")
    sessions = tuple(table.column("session").to_pylist())
    if list(sessions) != sorted(set(sessions)):
        raise IntegrityError("XNYS calendar sessions are not strictly unique and ordered")
    verification_receipt_id = sha256_bytes(
        canonical_json_bytes(
            {
                "release_id": manifest.release_id,
                "project": manifest.project,
                "dataset": manifest.dataset,
                "source_epoch": manifest.source_epoch,
                "role": manifest.role,
                "quality_state": manifest.quality_state,
                "event_start": manifest.event_start,
                "event_end": manifest.event_end,
                "schema_fingerprint": manifest.schema_fingerprint,
                "config_hash": manifest.config_hash,
                "environment_hash": manifest.environment_hash,
                "schedule_sha256": sha256_file(Path(release_directory) / "sessions.parquet"),
                "provenance": provenance,
            }
        )
    )
    calendar = PinnedSessionCalendar._from_verified_release_payload(
        release_id=manifest.release_id,
        sessions=sessions,
        verification_receipt_id=verification_receipt_id,
    )
    return LoadedExchangeCalendar(calendar=calendar, schedule=table, provenance=provenance)
