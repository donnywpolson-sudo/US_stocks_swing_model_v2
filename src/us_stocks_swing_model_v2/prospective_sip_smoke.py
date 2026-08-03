"""Plan-only prospective AAPL/SPY SIP smoke capture."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .clock import TrustedClock, require_trusted_clock
from .common import atomic_write, canonical_json_bytes, iso_z, parse_utc_z, require_contained_path, sha256_bytes, sha256_file
from .errors import ContractError, IntegrityError
from .exchange_calendar import load_xnys_calendar_release
from .identity import _load_identity_release_payload
from .providers.alpaca import AlpacaBarsPolicy, AlpacaBarsRequest, guarded_fetch_landed_pages
from .providers.network_execution import NetworkRequestPlan, start_local_network_execution
from .providers.snapshots import AsReceivedSnapshotStore, LandedSnapshot, NetworkAcquisitionRegistry
from .releases import AtomicReleasePublisher, build_manifest, verify_accepted_release


POLICY_PATH = "config/prospective_sip_smoke_policy.json"
IDENTITY_RELEASE_ID = "2c2898a6748dcd5b4d9f7875cd1549e050902c2f491005ed530a5899c685e115"
SMOKE_SOURCE = "alpaca_sip_canonical_bars"
PLAN_WORK_ROOT = "data/w/prospective_sip_smoke"
CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/prospective_sip_smoke.py",
    "src/us_stocks_swing_model_v2/cli/prospective_sip_smoke.py",
)


@dataclass(frozen=True)
class ProspectiveSmokeCandidate:
    acquisition_plan_id: str
    snapshot_id: str
    raw_sha256: str
    retrieved_at: datetime
    bars: tuple[dict[str, object], ...]
    candidate_id: str


def _clean_repository(root: Path) -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args], check=True, capture_output=True,
                text=True, encoding="utf-8", timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntegrityError("prospective SIP smoke requires a committed Git closure") from exc
    if Path(run("rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise IntegrityError("prospective SIP smoke Git root differs")
    if run("status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("prospective SIP smoke requires a clean committed tree")
    return {"commit": run("rev-parse", "HEAD"), "tree": run("rev-parse", "HEAD^{tree}")}


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    files = [{"path": item, "sha256": sha256_file(root / item)} for item in paths]
    return {"files": files, "sha256": sha256_bytes(canonical_json_bytes(files))}


def _source_binding(root: Path) -> dict[str, str]:
    path = root / "config/sources.json"
    try:
        sources = json.loads(path.read_text(encoding="utf-8"))
        sip = sources["sources"]["alpaca_basic_delayed_sip"]
        request_contract = sip["request_contract"]
        receipt_relative = sip["qualification_receipt"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ContractError("prospective SIP smoke source configuration is unreadable") from exc
    if (
        request_contract.get("qualified_feed") != "sip"
        or sip.get("status") != "qualified_sip_not_active"
        or sip.get("enabled_for_active_pipeline") is not False
        or not isinstance(receipt_relative, str)
    ):
        raise ContractError("prospective SIP smoke source state differs")
    receipt = require_contained_path(root / receipt_relative, root / "data", must_exist=True)
    if not receipt.is_file() or receipt.is_symlink():
        raise ContractError("prospective SIP qualification receipt differs")
    return {
        "sources_path": "config/sources.json",
        "sources_sha256": sha256_file(path),
        "qualification_receipt": receipt_relative.replace("\\", "/"),
        "qualification_receipt_sha256": sha256_file(receipt),
    }


def _load_policy(root: Path) -> dict[str, Any]:
    try:
        policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("prospective SIP smoke policy is unreadable") from exc
    expected = {"schema_version", "project", "mode", "identity_release_id", "symbols", "session", "earliest_capture_at", "request_contract", "network_registry"}
    contract = {
        "endpoint": "https://data.alpaca.markets/v2/stocks/bars", "feed": "sip",
        "timeframe": "1Day", "adjustment": "raw", "asof": None, "sort": "asc",
        "limit": 10000, "timeout_seconds": 30, "host_timeout_seconds": 120,
        "max_pages": 1, "max_response_bytes": 1048576,
    }
    if (
        type(policy) is not dict or set(policy) != expected or policy["schema_version"] != 1
        or policy["project"] != "US_stocks_swing_model_v2"
        or policy["mode"] != "PROSPECTIVE_TWO_SYMBOL_SIP_SMOKE_PLAN_ONLY"
        or policy["identity_release_id"] != IDENTITY_RELEASE_ID
        or policy["symbols"] != ["AAPL", "SPY"] or policy["session"] != "2026-08-03"
        or policy["earliest_capture_at"] != "2026-08-03T20:20:00Z"
        or policy["request_contract"] != contract
        or policy["network_registry"] != "config/alpaca_canonical_bars_network_registry.json"
    ):
        raise ContractError("prospective SIP smoke policy differs")
    return policy


def build_prospective_sip_smoke_plan(*, identity_release_directory: Path, calendar_release_directory: Path, repository_root: Path, clock: TrustedClock | None = None, allow_synthetic: bool = False) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = _load_policy(root)
    repository = _clean_repository(root)
    source_binding = _source_binding(root)
    trusted_clock = require_trusted_clock(clock)
    if not allow_synthetic and not trusted_clock.trust_eligible:
        raise ContractError("prospective SIP smoke planning requires production system UTC")
    identity_dir = Path(identity_release_directory)
    identity_manifest = verify_accepted_release(identity_dir, accepted_root=root / "data/vault/accepted")
    if (
        identity_manifest.release_id != policy["identity_release_id"] or identity_manifest.dataset != "identity"
        or identity_manifest.role != "prospective_as_received" or identity_manifest.quality_state != "PASS"
        or identity_manifest.source_epoch != "nasdaq_alpaca_active_us_equity_v1"
    ):
        raise IntegrityError("prospective SIP smoke identity release differs")
    snapshots = _load_identity_release_payload(identity_dir, identity_manifest.row_count)
    if len(snapshots) != 1:
        raise IntegrityError("prospective SIP smoke requires one identity snapshot")
    assets: dict[str, str] = {}
    for row in snapshots[0].rows:
        if row.symbol in policy["symbols"] and row.active and row.eligible and row.membership_present and row.abstention_reason is None:
            if row.symbol in assets:
                raise IntegrityError("prospective SIP smoke asset identity is ambiguous")
            assets[row.symbol] = row.asset_id
    if set(assets) != set(policy["symbols"]):
        raise IntegrityError("prospective SIP smoke identity is incomplete")
    calendar = load_xnys_calendar_release(calendar_release_directory, accepted_release_root=root / "data/vault/accepted")
    session = date.fromisoformat(policy["session"])
    rows = [row for row in calendar.schedule.to_pylist() if row["session"] == session]
    if len(rows) != 1 or iso_z(rows[0]["close_at"]) != "2026-08-03T20:00:00Z":
        raise IntegrityError("prospective SIP smoke pinned calendar session differs")
    earliest = parse_utc_z(policy["earliest_capture_at"], "prospective_sip_smoke.earliest_capture_at")
    now = trusted_clock.now()
    if now < earliest:
        raise ContractError("prospective SIP smoke capture is not yet available")
    request_contract = policy["request_contract"]
    request = AlpacaBarsRequest(tuple(policy["symbols"]), parse_utc_z("2026-08-03T04:00:00Z", "prospective_sip_smoke.start"), parse_utc_z("2026-08-03T20:00:00Z", "prospective_sip_smoke.end"), now, limit=10000)
    bars_policy = AlpacaBarsPolicy(feed="sip", timeframe="1Day", adjustment="raw", asof=None, sort="asc", minimum_end_lag_minutes=20, endpoint=request_contract["endpoint"])
    registry = NetworkAcquisitionRegistry.load(root / policy["network_registry"], allowed_root=root / "config")
    network = NetworkRequestPlan.create(registry=registry, source=SMOKE_SOURCE, initial_url=request.url(bars_policy), timeout_seconds=30, max_response_bytes=1048576, max_pages=1, pagination_parameter="page_token")
    unsigned = {
        "schema_version": 1, "mode": "PROSPECTIVE_TWO_SYMBOL_SIP_SMOKE_PLAN_ONLY",
        "repository": repository, "code_closure": _closure(root, CODE_CLOSURE_PATHS),
        "source_binding": source_binding,
        "identity": {"release_id": identity_manifest.release_id, "snapshot_id": snapshots[0].snapshot_id, "asset_ids": dict(sorted(assets.items()))},
        "calendar": {"release_id": calendar.calendar.release_id, "session": policy["session"], "close_at": "2026-08-03T20:00:00Z"},
        "request": {"method": "GET", "url": network.initial_url, "symbols": policy["symbols"], "start": "2026-08-03T04:00:00Z", "end": "2026-08-03T20:00:00Z", "feed": "sip", "timeframe": "1Day", "adjustment": "raw", "asof": None, "sort": "asc", "limit": 10000},
        "network_request_plan": network.as_dict(), "host_timeout_seconds": 120,
        "earliest_capture_at": policy["earliest_capture_at"], "requested_at": iso_z(now),
        "policy_sha256": sha256_file(root / POLICY_PATH), "environment_sha256": sha256_file(root / "config/environment.lock.json"),
        "authorities": {"network_calls": 0, "snapshot_write": False, "canonical_candidate": False, "canonical_release_publication": False, "research": False},
        "prohibitions": ["legacy_identity_release", "legacy_july_fixture", "proxy_input", "source_activation", "training_or_evaluation"],
    }
    return {**unsigned, "prospective_sip_smoke_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _request_from_plan(plan: dict[str, object]) -> tuple[AlpacaBarsRequest, AlpacaBarsPolicy, NetworkRequestPlan]:
    expected = {"schema_version", "mode", "repository", "code_closure", "source_binding", "identity", "calendar", "request", "network_request_plan", "host_timeout_seconds", "earliest_capture_at", "requested_at", "policy_sha256", "environment_sha256", "authorities", "prohibitions", "prospective_sip_smoke_plan_id"}
    if set(plan) != expected:
        raise ContractError("prospective smoke plan fields differ")
    unsigned = {key: value for key, value in plan.items() if key != "prospective_sip_smoke_plan_id"}
    if plan["prospective_sip_smoke_plan_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("prospective smoke plan ID differs")
    request_data = plan["request"]
    if not isinstance(request_data, dict):
        raise ContractError("prospective smoke request is invalid")
    request = AlpacaBarsRequest(tuple(request_data["symbols"]), parse_utc_z(request_data["start"], "smoke.start"), parse_utc_z(request_data["end"], "smoke.end"), parse_utc_z(plan["requested_at"], "smoke.requested_at"), limit=request_data["limit"])
    policy = AlpacaBarsPolicy(feed="sip", timeframe="1Day", adjustment="raw", asof=None, sort="asc", minimum_end_lag_minutes=20, endpoint="https://data.alpaca.markets/v2/stocks/bars")
    network_data = plan["network_request_plan"]
    if not isinstance(network_data, dict) or network_data.get("initial_url") != request.url(policy) or network_data.get("max_pages") != 1:
        raise IntegrityError("prospective smoke network binding differs")
    try:
        return request, policy, NetworkRequestPlan(**network_data)
    except TypeError as exc:
        raise IntegrityError("prospective smoke network plan fields differ") from exc


def write_prospective_sip_smoke_plan_package(*, plan: dict[str, object], repository_root: Path) -> dict[str, object]:
    """Atomically persist one exact request plan; caller authorization is separate."""
    root = Path(repository_root).resolve(strict=True)
    _request_from_plan(plan)
    if plan.get("repository") != _clean_repository(root):
        raise IntegrityError("prospective SIP smoke plan repository closure differs")
    if plan.get("code_closure") != _closure(root, CODE_CLOSURE_PATHS):
        raise IntegrityError("prospective SIP smoke plan code closure differs")
    if plan.get("source_binding") != _source_binding(root):
        raise IntegrityError("prospective SIP smoke plan source binding differs")
    if plan.get("policy_sha256") != sha256_file(root / POLICY_PATH) or plan.get("environment_sha256") != sha256_file(root / "config/environment.lock.json"):
        raise IntegrityError("prospective SIP smoke plan configuration differs")
    plan_id = str(plan["prospective_sip_smoke_plan_id"])
    output_root = require_contained_path(root / PLAN_WORK_ROOT, root / "data", must_exist=False)
    final = output_root / plan_id
    if final.exists():
        raise ContractError("prospective SIP smoke plan ID already exists")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".smoke-plan-", dir=output_root))
    try:
        plan_path = temporary / "request_plan.json"
        atomic_write(plan_path, canonical_json_bytes(plan))
        receipt = {
            "schema_version": 1,
            "mode": "PROSPECTIVE_SIP_SMOKE_LOCAL_PLAN_RECEIPT",
            "prospective_sip_smoke_plan_id": plan_id,
            "request_plan_sha256": sha256_file(plan_path),
            "repository": plan["repository"],
            "code_closure_sha256": plan["code_closure"]["sha256"],
            "execution_authorized": False,
        }
        atomic_write(temporary / "receipt.json", canonical_json_bytes(receipt))
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise IntegrityError("prospective SIP smoke plan package reload differs")
        os.replace(temporary, final)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return {"directory": str(final), "plan": plan, "receipt": receipt}


def load_prospective_sip_smoke_plan_package(*, plan_package: Path, repository_root: Path) -> dict[str, object]:
    """Reload and revalidate a persisted plan without rebuilding it."""
    root = Path(repository_root).resolve(strict=True)
    package = require_contained_path(plan_package, root / "data", must_exist=True)
    plan_path, receipt_path = package / "request_plan.json", package / "receipt.json"
    if package.is_symlink() or not plan_path.is_file() or not receipt_path.is_file():
        raise ContractError("prospective SIP smoke plan package is incomplete")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("prospective SIP smoke plan package is unreadable") from exc
    _request_from_plan(plan)
    plan_id = plan["prospective_sip_smoke_plan_id"]
    if (
        receipt != {
            "schema_version": 1,
            "mode": "PROSPECTIVE_SIP_SMOKE_LOCAL_PLAN_RECEIPT",
            "prospective_sip_smoke_plan_id": plan_id,
            "request_plan_sha256": sha256_file(plan_path),
            "repository": plan["repository"],
            "code_closure_sha256": plan["code_closure"]["sha256"],
            "execution_authorized": False,
        }
    ):
        raise IntegrityError("prospective SIP smoke plan package receipt differs")
    if plan.get("repository") != _clean_repository(root):
        raise IntegrityError("prospective SIP smoke plan repository closure differs")
    if plan.get("code_closure") != _closure(root, CODE_CLOSURE_PATHS):
        raise IntegrityError("prospective SIP smoke plan code closure differs")
    if plan.get("source_binding") != _source_binding(root):
        raise IntegrityError("prospective SIP smoke plan source binding differs")
    if plan.get("policy_sha256") != sha256_file(root / POLICY_PATH) or plan.get("environment_sha256") != sha256_file(root / "config/environment.lock.json"):
        raise IntegrityError("prospective SIP smoke plan configuration differs")
    return plan


def execute_prospective_sip_smoke_capture(*, plan_package: Path, approved_plan_id: str, api_key_id: str, api_secret_key: str, clock: TrustedClock, repository_root: Path) -> LandedSnapshot:
    """Perform the one permitted request. Caller authorization is separate."""
    if os.environ.get("FREE_SOURCE_QUALIFICATION_APPROVED") != "YES":
        raise PermissionError("prospective smoke network approval is absent")
    if not api_key_id or not api_secret_key:
        raise PermissionError("prospective smoke credentials are absent")
    root = Path(repository_root).resolve(strict=True)
    plan = load_prospective_sip_smoke_plan_package(plan_package=plan_package, repository_root=root)
    if plan.get("prospective_sip_smoke_plan_id") != approved_plan_id:
        raise PermissionError("approved prospective smoke plan differs")
    request, policy, network = _request_from_plan(plan)
    trusted = require_trusted_clock(clock)
    if trusted.now() < parse_utc_z(plan["earliest_capture_at"], "smoke.earliest_capture_at"):
        raise ContractError("prospective SIP smoke capture is not yet available")
    registry = NetworkAcquisitionRegistry.load(root / "config/alpaca_canonical_bars_network_registry.json", allowed_root=root / "config")
    session = start_local_network_execution(network, registry=registry, clock=trusted)
    store = AsReceivedSnapshotStore(root / "data/vault/qualification/as_received/prospective_sip_smoke", allowed_root=root / "data", acquisition_registry=registry)
    deadline = time.monotonic() + int(plan["host_timeout_seconds"])
    if time.monotonic() >= deadline:
        raise TimeoutError("prospective SIP smoke exceeded its host timeout before transport")
    pages = guarded_fetch_landed_pages(request, policy=policy, snapshot_store=store, api_key_id=api_key_id, api_secret_key=api_secret_key, network_enabled=True, max_pages=1, timeout_seconds=30, max_response_bytes=1048576, clock=trusted, authorization_session=session, source=SMOKE_SOURCE)
    if time.monotonic() > deadline:
        raise TimeoutError("prospective SIP smoke exceeded its fixed host timeout")
    if len(pages) != 1:
        raise IntegrityError("prospective smoke capture exceeded one request")
    return pages[0]


def build_prospective_sip_smoke_candidate(snapshot: LandedSnapshot, *, plan: dict[str, object]) -> ProspectiveSmokeCandidate:
    request, _, network = _request_from_plan(plan)
    if snapshot.source != SMOKE_SOURCE or snapshot.request_plan_id != network.plan_id or snapshot.url != plan["request"]["url"] or snapshot.http_status != 200 or not snapshot.trust_eligible:
        raise IntegrityError("prospective smoke snapshot binding differs")
    try:
        payload = json.loads(snapshot.read_verified_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("prospective smoke raw response is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"bars", "next_page_token"} or payload["next_page_token"] is not None or not isinstance(payload["bars"], dict) or set(payload["bars"]) != {"AAPL", "SPY"}:
        raise ContractError("prospective smoke response shape or pagination differs")
    rows: list[dict[str, object]] = []
    for symbol in ("AAPL", "SPY"):
        bars = payload["bars"][symbol]
        if not isinstance(bars, list) or len(bars) != 1 or not isinstance(bars[0], dict):
            raise ContractError("prospective smoke requires one exact daily bar per symbol")
        row = bars[0]
        if set(row) - {"t", "o", "h", "l", "c", "v", "n", "vw"} or not {"t", "o", "h", "l", "c", "v"}.issubset(row):
            raise ContractError("prospective smoke bar schema differs")
        at = parse_utc_z(row["t"], "smoke.bar.t")
        if at.date() != date(2026, 8, 3) or not all(isinstance(row[key], (int, float)) and not isinstance(row[key], bool) and row[key] > 0 for key in ("o", "h", "l", "c")) or type(row["v"]) is not int or row["v"] < 0:
            raise ContractError("prospective smoke bar differs from the frozen session contract")
        rows.append({"symbol": symbol, "asset_id": plan["identity"]["asset_ids"][symbol], "session": "2026-08-03", "open": row["o"], "high": row["h"], "low": row["l"], "close": row["c"], "volume": row["v"], "available_at": iso_z(snapshot.retrieved_at)})
    unsigned = {"acquisition_plan_id": plan["prospective_sip_smoke_plan_id"], "snapshot_id": snapshot.snapshot_id, "raw_sha256": snapshot.raw_sha256, "retrieved_at": iso_z(snapshot.retrieved_at), "bars": rows}
    return ProspectiveSmokeCandidate(plan["prospective_sip_smoke_plan_id"], snapshot.snapshot_id, snapshot.raw_sha256, snapshot.retrieved_at, tuple(rows), sha256_bytes(canonical_json_bytes(unsigned)))


def build_prospective_sip_smoke_publication_plan(candidate: ProspectiveSmokeCandidate, *, plan: dict[str, object], accepted_root: Path, work_root: Path) -> dict[str, object]:
    if candidate.acquisition_plan_id != plan.get("prospective_sip_smoke_plan_id") or len(candidate.bars) != 2:
        raise ContractError("prospective smoke candidate differs from its plan")
    unsigned = {"schema_version": 1, "mode": "PROSPECTIVE_SIP_SMOKE_PUBLICATION_PLAN_ONLY", "candidate_id": candidate.candidate_id, "acquisition_plan_id": candidate.acquisition_plan_id, "snapshot_id": candidate.snapshot_id, "accepted_root": str(Path(accepted_root).resolve()), "work_root": str(Path(work_root).resolve()), "dataset": "alpaca_daily_bars", "role": "prospective_as_received", "quality_state": "PASS", "upstream_release_ids": [plan["identity"]["release_id"], plan["calendar"]["release_id"]], "source_activation": False, "publication_authorized": False, "research": False}
    return {**unsigned, "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def publish_prospective_sip_smoke(*, candidate: ProspectiveSmokeCandidate, plan: dict[str, object], approved_publication_plan_id: str, owner_confirmation: str, accepted_root: Path, work_root: Path) -> Path:
    """Publish the one non-active smoke release only under a later exact gate."""
    if owner_confirmation != "YES" or os.environ.get("PROSPECTIVE_SIP_SMOKE_PUBLICATION_APPROVED") != "YES":
        raise PermissionError("prospective smoke publication confirmation is absent")
    publication = build_prospective_sip_smoke_publication_plan(candidate, plan=plan, accepted_root=accepted_root, work_root=work_root)
    if publication["publication_plan_id"] != approved_publication_plan_id:
        raise PermissionError("approved prospective smoke publication plan differs")
    work = Path(work_root).resolve(); work.mkdir(parents=True, exist_ok=True)
    stage = work / approved_publication_plan_id
    if stage.exists():
        raise IntegrityError("prospective smoke publication staging collision")
    stage.mkdir()
    rows = [{"asset_id": row["asset_id"], "session": row["session"], "open": row["open"], "close": row["close"], "available_at": row["available_at"], "halted": False, "delisted": False} for row in candidate.bars]
    payload = canonical_json_bytes({"schema_version": 1, "rows": rows})
    receipt = canonical_json_bytes({"schema_version": 1, "receipt_class": "PROSPECTIVE_SIP_SMOKE", "candidate_id": candidate.candidate_id, "acquisition_plan_id": candidate.acquisition_plan_id, "snapshot_id": candidate.snapshot_id, "raw_sha256": candidate.raw_sha256, "identity": plan["identity"], "calendar": plan["calendar"], "source_activation": False})
    atomic_write(stage / "daily_bars.json", payload); atomic_write(stage / "prospective_sip_smoke_receipt.json", receipt)
    manifest = build_manifest(stage, ("daily_bars.json", "prospective_sip_smoke_receipt.json"), project="US_stocks_swing_model_v2", dataset="alpaca_daily_bars", source_epoch="alpaca_basic_sip_raw_v1", role="prospective_as_received", quality_state="PASS", created_at=iso_z(candidate.retrieved_at), row_count=2, event_start="2026-08-03", event_end="2026-08-03", upstream_release_ids=(plan["identity"]["release_id"], plan["calendar"]["release_id"]), schema_fingerprint=sha256_bytes(canonical_json_bytes({"daily_bars": 1, "smoke": 1})), code_hash=sha256_file(Path(__file__)), config_hash=plan["policy_sha256"], environment_hash=plan["environment_sha256"])
    return AtomicReleasePublisher(Path(accepted_root).resolve()).publish(stage, manifest)
