from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from .clock import TrustedClock, require_trusted_clock
from .common import (
    atomic_write,
    canonical_json_bytes,
    iso_z,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from .errors import ContractError, IntegrityError
from .exchange_calendar import load_xnys_calendar_release
from .releases import verify_accepted_release


PROFILE_ID = "ALPACA_FREE_BOUNDED_V1"
PROFILE_PATH = "config/alpaca_free_bounded_v1.json"
REGISTRY_PATH = "config/alpaca_free_bounded_network_registry.json"
REQUESTED_START = date(2016, 1, 1)
CALENDAR_CUTOVER_CONFIRMATION = "YES"


class EvidenceClass(str, Enum):
    HISTORICAL_RECONSTRUCTED = "HISTORICAL_RECONSTRUCTED"
    PROSPECTIVE_AS_OBSERVED = "PROSPECTIVE_AS_OBSERVED"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


def load_profile(repository_root: Path) -> dict[str, object]:
    path = Path(repository_root) / PROFILE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("ALPACA_FREE_BOUNDED_V1 profile is unavailable") from exc
    if not isinstance(payload, dict):
        raise ContractError("ALPACA_FREE_BOUNDED_V1 profile must be a JSON object")
    required = {
        "schema_version",
        "profile_id",
        "project",
        "selection",
        "strict_reference",
        "data_budget",
        "requested_raw_data_start",
        "actual_source_start",
        "effective_research_start",
        "evidence_classes",
        "calendar",
        "bars",
        "historical_universe_candidate",
        "prospective_identity",
        "corporate_actions",
        "universe",
        "outcome",
        "historical_short_labels",
        "prospective_short_gate",
        "prospective_capture",
        "readiness",
        "credentials",
        "prohibitions",
    }
    if set(payload) != required:
        raise ContractError("ALPACA_FREE_BOUNDED_V1 profile fields differ")
    if (
        payload["schema_version"] != 1
        or payload["profile_id"] != PROFILE_ID
        or payload["project"] != "US_stocks_swing_model_v2"
        or payload["selection"] != "OPT_IN_PROFILE_ONLY"
        or payload["data_budget"] != "COMPLETELY_FREE_ONLY"
        or payload["requested_raw_data_start"] != REQUESTED_START.isoformat()
    ):
        raise ContractError("ALPACA_FREE_BOUNDED_V1 identity or budget drifted")
    bars = payload["bars"]
    universe = payload["universe"]
    outcome = payload["outcome"]
    short_gate = payload["prospective_short_gate"]
    strict = payload["strict_reference"]
    credentials = payload["credentials"]
    calendar = payload["calendar"]
    capture = payload["prospective_capture"]
    if not all(isinstance(item, dict) for item in (bars, universe, outcome, short_gate, strict, calendar, capture)):
        raise ContractError("ALPACA_FREE_BOUNDED_V1 sections must be objects")
    if bars != {
        **bars,
        "provider": "alpaca",
        "required_feed": "sip",
        "timeframe": "1Day",
        "adjustment": "raw",
        "sort": "asc",
        "asof": None,
    }:
        raise ContractError("free bounded bars contract drifted")
    if (
        type(bars.get("minimum_end_lag_minutes")) is not int
        or bars["minimum_end_lag_minutes"] < 15
        or bars.get("mixing_iex_and_sip") != "PROHIBITED"
        or bars.get("pagination_must_be_terminal") is not True
    ):
        raise ContractError("free delayed SIP safeguards are incomplete")
    if (
        universe.get("information_cutoff") != "THROUGH_T_MINUS_1"
        or universe.get("primary_size") != 500
        or universe.get("sensitivity_size") != 1000
        or universe.get("lookback_sessions") != 60
        or universe.get("minimum_previous_close") != 5.0
    ):
        raise ContractError("bounded universe contract drifted")
    if (
        outcome.get("name")
        != "ALPACA_SIP_5_SESSION_LONG_SHORT_GROSS_RETURN_EX_BORROW_COSTS"
        or outcome.get("supported_sides") != ["LONG", "SHORT"]
        or outcome.get("stock_borrow_fee") != 0.0
        or outcome.get("locate_fee") != 0.0
        or outcome.get("cost_label") != "GROSS OF STOCK-BORROW AND LOCATE COSTS"
        or outcome.get("unresolved_short_buy_in_multiples") != [2.0, 3.0, 5.0]
    ):
        raise ContractError("long/short outcome contract drifted")
    if (
        short_gate.get("required_borrow_status") != "easy_to_borrow"
        or short_gate.get("whole_shares_only") is not True
        or short_gate.get("round_up") is not False
        or strict.get("commit") != "c29e244174940f76babf75bcf91bbd11ca470c46"
        or strict.get("preserved_outside_profile") is not True
    ):
        raise ContractError("short gate or strict-reference binding drifted")
    if credentials != {
        "alpaca": ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"],
        "alpha_vantage": ["ALPHA_VANTAGE_API_KEY"],
        "storage": "ENVIRONMENT_ONLY",
    }:
        raise ContractError("free bounded credential variable contract drifted")
    _validate_calendar_config(calendar)
    if (
        capture.get("schema_version") != 1
        or capture.get("pre_decision_phase_sources") != [
            "alpaca_free_bounded_assets",
            "nasdaq_free_bounded_listed",
            "nasdaq_free_bounded_otherlisted",
        ]
        or capture.get("completed_session_phase_sources") != [
            "alpaca_free_bounded_bars",
            "alpaca_free_bounded_corporate_actions",
        ]
        or capture.get("automation_acceptance_policy_id")
        != "TWO_SESSION_AUTOMATION_ACCEPTANCE_V1"
        or capture.get("automation_acceptance_required_consecutive_sessions") != 2
        or capture.get("background_reliability_monitor_policy_id")
        != "NONBLOCKING_BACKGROUND_RELIABILITY_MONITOR"
        or capture.get("background_reliability_monitor_window_sessions") != 20
        or capture.get("background_reliability_monitor_blocking") is not False
        or capture.get("prospective_research_promotion") is not False
        or capture.get("training_promotion") is not False
        or capture.get("evaluation_promotion") is not False
    ):
        raise ContractError("prospective capture policy drifted")
    return payload


def _validate_calendar_config(calendar: Mapping[str, object]) -> None:
    expected = {
        "name", "accepted_root", "strict_release_id",
        "successor_candidate_release_id", "qualified_release_id",
        "qualification", "strict_binding_unchanged",
    }
    if (
        set(calendar) != expected
        or calendar.get("name") != "XNYS"
        or calendar.get("accepted_root") != "data/vault/accepted"
        or calendar.get("strict_binding_unchanged") is not True
    ):
        raise ContractError("profile calendar configuration drifted")
    strict_id = require_sha256(calendar.get("strict_release_id"), "strict calendar release ID")
    successor_id = require_sha256(
        calendar.get("successor_candidate_release_id"),
        "successor calendar release ID",
    )
    if strict_id == successor_id:
        raise ContractError("calendar successor must differ from the strict release")
    qualified = calendar.get("qualified_release_id")
    receipt = calendar.get("qualification")
    if qualified is None and receipt is None:
        return
    if require_sha256(qualified, "qualified calendar release ID") != successor_id:
        raise ContractError("profile selected an unqualified calendar successor")
    if not isinstance(receipt, dict):
        raise ContractError("profile calendar qualification receipt is missing")
    receipt_id = require_sha256(receipt.get("qualification_id"), "calendar qualification ID")
    unsigned = {key: value for key, value in receipt.items() if key != "qualification_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != receipt_id:
        raise IntegrityError("calendar qualification ID differs from canonical content")
    if (
        receipt.get("status") != "QUALIFIED_BYTE_IDENTICAL_SUCCESSOR"
        or receipt.get("old_release_id") != strict_id
        or receipt.get("successor_release_id") != successor_id
        or receipt.get("session_payload_comparison") != "BYTE_IDENTICAL"
        or receipt.get("session_difference_count") != 0
        or receipt.get("old_sessions_sha256") != receipt.get("successor_sessions_sha256")
        or receipt.get("strict_binding_unchanged") is not True
        or receipt.get("source_activation") is not False
        or receipt.get("research_authorized") is not False
    ):
        raise IntegrityError("calendar qualification receipt does not authorize this profile cutover")


def _calendar_repository_binding(root: Path) -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args], check=True, capture_output=True,
                text=True, encoding="utf-8", timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntegrityError("calendar qualification requires a valid Git closure") from exc
    if Path(run("rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise IntegrityError("calendar qualification Git root differs")
    if run("branch", "--show-current") != "alpaca-free-bounded-long-short":
        raise IntegrityError("calendar qualification requires the bounded profile branch")
    if run("status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("calendar qualification requires a clean committed tree")
    return {
        "branch": "alpaca-free-bounded-long-short",
        "commit": run("rev-parse", "HEAD"),
        "tree": run("rev-parse", "HEAD^{tree}"),
    }


def build_calendar_qualification_plan(
    *, repository_root: Path,
) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    profile = load_profile(root)
    calendar = profile["calendar"]
    if calendar["qualified_release_id"] is not None:
        raise ContractError("profile calendar successor is already qualified")
    repository = _calendar_repository_binding(root)
    accepted_root = root / str(calendar["accepted_root"])
    old_directory = accepted_root / "xnys_sessions" / str(calendar["strict_release_id"])
    successor_directory = accepted_root / "xnys_sessions" / str(calendar["successor_candidate_release_id"])
    old = verify_accepted_release(old_directory, accepted_root=accepted_root)
    successor = verify_accepted_release(successor_directory, accepted_root=accepted_root)
    loaded = load_xnys_calendar_release(
        successor_directory,
        accepted_release_root=accepted_root,
    )
    old_sessions = old_directory / "sessions.parquet"
    successor_sessions = successor_directory / "sessions.parquet"
    old_hash = sha256_file(old_sessions)
    successor_hash = sha256_file(successor_sessions)
    old_table = pq.read_table(old_sessions)
    successor_table = pq.read_table(successor_sessions)
    difference_count = 0 if old_table.equals(successor_table) else max(
        old_table.num_rows, successor_table.num_rows
    )
    byte_identical = old_sessions.read_bytes() == successor_sessions.read_bytes()
    if (
        old.release_id != calendar["strict_release_id"]
        or successor.release_id != calendar["successor_candidate_release_id"]
        or not byte_identical
        or difference_count != 0
        or old_hash != successor_hash
        or old.row_count != successor.row_count
        or loaded.calendar.release_id != successor.release_id
    ):
        raise IntegrityError("calendar successor is not a byte-identical canonical successor")
    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_FREE_BOUNDED_XNYS_QUALIFICATION_AND_CUTOVER_PLAN",
        "repository": repository,
        "profile_path": PROFILE_PATH,
        "profile_sha256": sha256_file(root / PROFILE_PATH),
        "accepted_root": str(accepted_root),
        "old_release_id": old.release_id,
        "successor_release_id": successor.release_id,
        "old_environment_sha256": old.environment_hash,
        "successor_environment_sha256": successor.environment_hash,
        "old_sessions_sha256": old_hash,
        "successor_sessions_sha256": successor_hash,
        "old_session_count": old.row_count,
        "successor_session_count": successor.row_count,
        "first_session": successor.event_start,
        "last_session": successor.event_end,
        "session_payload_comparison": "BYTE_IDENTICAL",
        "session_difference_count": 0,
        "original_release_recoverable": old_directory.is_dir(),
        "strict_binding_unchanged": True,
        "source_activation": False,
        "research_authorized": False,
    }
    return {
        **unsigned,
        "qualification_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def execute_calendar_qualification_cutover(
    *,
    repository_root: Path,
    approved_plan_id: str,
    owner_confirmation: str,
    clock: TrustedClock,
) -> dict[str, object]:
    if owner_confirmation != CALENDAR_CUTOVER_CONFIRMATION:
        raise PermissionError("calendar qualification owner confirmation differs")
    trusted = require_trusted_clock(clock)
    if not trusted.trust_eligible:
        raise PermissionError("calendar qualification requires production system UTC")
    root = Path(repository_root).resolve(strict=True)
    plan = build_calendar_qualification_plan(repository_root=root)
    if plan["qualification_plan_id"] != approved_plan_id:
        raise PermissionError("approved calendar qualification plan differs")
    receipt_unsigned = {
        "schema_version": 1,
        "status": "QUALIFIED_BYTE_IDENTICAL_SUCCESSOR",
        "qualified_at": iso_z(trusted.now()),
        "qualification_plan_id": approved_plan_id,
        "old_release_id": plan["old_release_id"],
        "successor_release_id": plan["successor_release_id"],
        "old_environment_sha256": plan["old_environment_sha256"],
        "successor_environment_sha256": plan["successor_environment_sha256"],
        "old_sessions_sha256": plan["old_sessions_sha256"],
        "successor_sessions_sha256": plan["successor_sessions_sha256"],
        "session_count": plan["successor_session_count"],
        "first_session": plan["first_session"],
        "last_session": plan["last_session"],
        "session_payload_comparison": "BYTE_IDENTICAL",
        "session_difference_count": 0,
        "original_release_recoverable": True,
        "strict_binding_unchanged": True,
        "source_activation": False,
        "research_authorized": False,
    }
    receipt = {
        **receipt_unsigned,
        "qualification_id": sha256_bytes(canonical_json_bytes(receipt_unsigned)),
    }
    profile = load_profile(root)
    profile["calendar"]["qualified_release_id"] = plan["successor_release_id"]
    profile["calendar"]["qualification"] = receipt
    atomic_write(root / PROFILE_PATH, json.dumps(profile, indent=2).encode("utf-8") + b"\n")
    load_profile(root)
    return receipt


def load_qualified_profile_calendar(*, repository_root: Path):
    root = Path(repository_root).resolve(strict=True)
    profile = load_profile(root)
    calendar = profile["calendar"]
    if calendar["qualified_release_id"] is None:
        raise ContractError("CALENDAR_NOT_QUALIFIED")
    release = (
        root / str(calendar["accepted_root"]) / "xnys_sessions"
        / str(calendar["qualified_release_id"])
    )
    loaded = load_xnys_calendar_release(
        release,
        accepted_release_root=root / str(calendar["accepted_root"]),
    )
    receipt = calendar["qualification"]
    if (
        sha256_file(release / "sessions.parquet") != receipt["successor_sessions_sha256"]
        or loaded.calendar.release_id != calendar["qualified_release_id"]
    ):
        raise IntegrityError("qualified profile calendar release differs")
    strict = (
        root / str(calendar["accepted_root"]) / "xnys_sessions"
        / str(calendar["strict_release_id"])
    )
    if (
        not strict.is_dir()
        or sha256_file(strict / "sessions.parquet") != receipt["old_sessions_sha256"]
    ):
        raise IntegrityError("original strict calendar release is not recoverable")
    return loaded


@dataclass(frozen=True)
class AcquisitionUnit:
    unit_id: str
    source: str
    symbols: tuple[str, ...]
    start: date
    end: date
    sanitized_url: str
    canonical_query: tuple[tuple[str, str], ...]
    maximum_pages: int
    evidence_class: EvidenceClass

    def receipt_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "source": self.source,
            "symbols": list(self.symbols),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "sanitized_url": self.sanitized_url,
            "canonical_query": [[key, value] for key, value in self.canonical_query],
            "maximum_pages": self.maximum_pages,
            "evidence_class": self.evidence_class.value,
        }


@dataclass(frozen=True)
class HistoricalBackfillPlan:
    profile_id: str
    requested_at: datetime
    requested_start: date
    requested_end: date
    units: tuple[AcquisitionUnit, ...]
    completed_unit_ids: tuple[str, ...]
    plan_id: str

    @property
    def pending_units(self) -> tuple[AcquisitionUnit, ...]:
        completed = set(self.completed_unit_ids)
        return tuple(unit for unit in self.units if unit.unit_id not in completed)

    def summary(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "plan_id": self.plan_id,
            "requested_at": iso_z(self.requested_at),
            "date_range": {
                "start": self.requested_start.isoformat(),
                "end": self.requested_end.isoformat(),
            },
            "symbol_count": len({symbol for unit in self.units for symbol in unit.symbols}),
            "batch_count": len({unit.symbols for unit in self.units}),
            "estimated_minimum_request_count": len(self.units),
            "maximum_request_count": sum(unit.maximum_pages for unit in self.units),
            "completed_checkpoints": len(self.completed_unit_ids),
            "pending_checkpoints": len(self.pending_units),
            "free_tier_constraints": {
                "feed": "sip",
                "minimum_end_lag_minutes": 20,
                "configured_requests_per_minute": 200,
                "full_pagination_required": True,
                "automatic_full_backfill": False,
            },
            "execution_authorized": False,
        }


def _canonical_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(value).strip().upper() for value in symbols if str(value).strip()}))
    if not normalized or any("," in value or not value.isascii() for value in normalized):
        raise ContractError("backfill symbols must be nonempty canonical ASCII symbols")
    return normalized


def _year_windows(start: date, end: date) -> tuple[tuple[date, date], ...]:
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        boundary = min(end, date(cursor.year, 12, 31))
        windows.append((cursor, boundary))
        cursor = boundary + timedelta(days=1)
    return tuple(windows)


def build_historical_backfill_plan(
    *,
    repository_root: Path,
    symbols: Iterable[str],
    requested_start: date,
    requested_end: date,
    requested_at: datetime,
    completed_unit_ids: Iterable[str] = (),
) -> HistoricalBackfillPlan:
    profile = load_profile(repository_root)
    requested_at = require_aware_utc(requested_at, "requested_at")
    if requested_start < REQUESTED_START:
        raise ContractError("free Alpaca history cannot be requested before 2016-01-01")
    if requested_end < requested_start:
        raise ContractError("backfill end cannot precede start")
    minimum_lag = int(profile["bars"]["minimum_end_lag_minutes"])
    if datetime.combine(requested_end + timedelta(days=1), datetime.min.time(), timezone.utc) > (
        requested_at - timedelta(minutes=minimum_lag)
    ):
        raise ContractError("backfill end is inside the configured free SIP delay boundary")
    canonical_symbols = _canonical_symbols(symbols)
    batch_size = int(profile["bars"]["deterministic_batch_size"])
    maximum_pages = int(profile["bars"]["maximum_pages_per_unit"])
    units: list[AcquisitionUnit] = []
    for offset in range(0, len(canonical_symbols), batch_size):
        batch = canonical_symbols[offset : offset + batch_size]
        for window_start, window_end in _year_windows(requested_start, requested_end):
            query = (
                ("symbols", ",".join(batch)),
                ("start", window_start.isoformat()),
                ("end", window_end.isoformat()),
                ("timeframe", "1Day"),
                ("adjustment", "raw"),
                ("feed", "sip"),
                ("sort", "asc"),
                ("limit", "10000"),
            )
            url = f"{profile['bars']['endpoint']}?{urlencode(query)}"
            unsigned = {
                "profile_id": PROFILE_ID,
                "source": "alpaca_free_bounded_bars",
                "symbols": list(batch),
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
                "sanitized_url": url,
                "canonical_query": [[key, value] for key, value in query],
                "maximum_pages": maximum_pages,
                "evidence_class": EvidenceClass.HISTORICAL_RECONSTRUCTED.value,
            }
            units.append(
                AcquisitionUnit(
                    unit_id=sha256_bytes(canonical_json_bytes(unsigned)),
                    source="alpaca_free_bounded_bars",
                    symbols=batch,
                    start=window_start,
                    end=window_end,
                    sanitized_url=url,
                    canonical_query=query,
                    maximum_pages=maximum_pages,
                    evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
                )
            )
    completed = tuple(sorted(set(completed_unit_ids)))
    known_units = {unit.unit_id for unit in units}
    if not set(completed) <= known_units:
        raise IntegrityError("checkpoint contains a unit outside the exact plan")
    unsigned_plan = {
        "profile_id": PROFILE_ID,
        "requested_at": iso_z(requested_at),
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "units": [unit.receipt_dict() for unit in units],
        "completed_unit_ids": list(completed),
    }
    return HistoricalBackfillPlan(
        profile_id=PROFILE_ID,
        requested_at=requested_at,
        requested_start=requested_start,
        requested_end=requested_end,
        units=tuple(units),
        completed_unit_ids=completed,
        plan_id=sha256_bytes(canonical_json_bytes(unsigned_plan)),
    )


@dataclass(frozen=True)
class RetryDisposition:
    state: str
    retryable: bool
    next_attempt: int | None
    delay_seconds: float | None
    reason: str


def retry_disposition(
    *,
    http_status: int,
    attempt_number: int,
    request_id: str,
    max_attempts: int = 4,
    base_seconds: float = 1.0,
    maximum_seconds: float = 30.0,
    retry_after_seconds: float | None = None,
) -> RetryDisposition:
    if type(http_status) is not int or not 100 <= http_status <= 599:
        raise ContractError("HTTP status is invalid")
    if type(attempt_number) is not int or not 1 <= attempt_number <= max_attempts:
        raise ContractError("attempt number is outside the bounded retry policy")
    if http_status in {400, 401, 403, 422}:
        return RetryDisposition("TERMINAL_FAILURE", False, None, None, f"HTTP_{http_status}")
    retryable = http_status == 429 or 500 <= http_status <= 599
    if not retryable:
        if 200 <= http_status <= 299:
            return RetryDisposition("ACCEPTABLE_RESPONSE", False, None, None, "HTTP_SUCCESS")
        return RetryDisposition("TERMINAL_FAILURE", False, None, None, f"HTTP_{http_status}")
    if attempt_number >= max_attempts:
        return RetryDisposition("RETRY_BUDGET_EXHAUSTED", False, None, None, f"HTTP_{http_status}")
    digest = int(sha256_bytes(canonical_json_bytes({"request_id": request_id, "attempt": attempt_number}))[:8], 16)
    jitter = 0.75 + (digest / 0xFFFFFFFF) * 0.5
    computed = min(maximum_seconds, base_seconds * (2 ** (attempt_number - 1)) * jitter)
    if retry_after_seconds is not None:
        if not math.isfinite(retry_after_seconds) or retry_after_seconds < 0:
            raise ContractError("retry-after value is invalid")
        computed = min(maximum_seconds, max(computed, retry_after_seconds))
    return RetryDisposition(
        "RETRY_REQUIRED_NEW_INVOCATION",
        True,
        attempt_number + 1,
        round(computed, 6),
        f"HTTP_{http_status}",
    )


def validate_bars_payload(
    payload: object,
    *,
    expected_symbols: Sequence[str],
    expected_sessions: Sequence[date] | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ContractError("Alpaca bars response root must be an object")
    unknown_top_level = sorted(set(payload) - {"bars", "next_page_token"})
    if unknown_top_level:
        return {
            "validation_status": "QUARANTINED_UNKNOWN_SCHEMA",
            "unknown_fields": unknown_top_level,
            "accepted": False,
        }
    bars = payload.get("bars")
    token = payload.get("next_page_token")
    if not isinstance(bars, dict) or (token is not None and (not isinstance(token, str) or not token)):
        raise ContractError("Alpaca bars response schema is invalid")
    expected = set(expected_symbols)
    if set(bars) - expected:
        raise ContractError("Alpaca bars response contains an unexpected symbol")
    first_last: dict[str, dict[str, str | None]] = {}
    missing_by_symbol: dict[str, list[str]] = {}
    unknown_bar_fields: set[str] = set()
    row_count = 0
    for symbol in expected_symbols:
        rows = bars.get(symbol, [])
        if not isinstance(rows, list):
            raise ContractError("Alpaca bars symbol payload must be a list")
        sessions: list[str] = []
        session_dates: list[date] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ContractError("Alpaca bar must be an object")
            required = {"t", "o", "h", "l", "c", "v"}
            if not required <= set(row):
                raise ContractError("Alpaca bar is missing required OHLCV fields")
            numeric = [row[key] for key in ("o", "h", "l", "c", "v")]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
                raise ContractError("Alpaca bar OHLCV values must be numeric")
            open_, high, low, close, volume = map(float, numeric)
            if (
                not all(math.isfinite(value) for value in (open_, high, low, close, volume))
                or min(open_, high, low, close) <= 0
                or volume < 0
                or high < low
                or not low <= open_ <= high
                or not low <= close <= high
            ):
                raise ContractError("Alpaca bar violates OHLCV invariants")
            timestamp = row["t"]
            if not isinstance(timestamp, str):
                raise ContractError("Alpaca bar timestamp must be exact text")
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                parsed_timestamp = require_aware_utc(parsed_timestamp, "bar.timestamp")
            except (ValueError, ContractError) as exc:
                raise ContractError("Alpaca bar timestamp is invalid") from exc
            local_timestamp = parsed_timestamp.astimezone(ZoneInfo("America/New_York"))
            if local_timestamp.time() != datetime.min.time():
                raise ContractError("Alpaca daily bar timestamp is not New York midnight")
            sessions.append(timestamp)
            session_dates.append(local_timestamp.date())
            unknown_bar_fields.update(set(row) - {"t", "o", "h", "l", "c", "v", "n", "vw"})
            row_count += 1
        if len(sessions) != len(set(sessions)):
            raise ContractError("Alpaca bars contain duplicate asset/session rows")
        if expected_sessions is not None:
            expected_set = set(expected_sessions)
            unexpected = sorted(set(session_dates) - expected_set)
            if unexpected:
                raise ContractError("Alpaca bars contain unexpected non-session records")
            missing_by_symbol[symbol] = [
                value.isoformat() for value in sorted(expected_set - set(session_dates))
            ]
        first_last[symbol] = {
            "first_available": min(sessions) if sessions else None,
            "last_available": max(sessions) if sessions else None,
        }
    return {
        "validation_status": "PASS",
        "unknown_fields": [],
        "accepted": True,
        "terminal_page": token is None,
        "next_page_token": token,
        "row_count": row_count,
        "coverage": first_last,
        "missing_expected_sessions": missing_by_symbol,
        "unknown_bar_fields_preserved_in_raw": sorted(unknown_bar_fields),
    }
