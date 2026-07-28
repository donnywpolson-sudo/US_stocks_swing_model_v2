from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.request import Request

from ..clock import TrustedClock, require_trusted_clock
from ..common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..environment import validate_environment_lock
from ..errors import ContractError, IntegrityError, NetworkGuardError
from ..identity import (
    AlpacaAssetProjection,
    IdentitySnapshot,
    merge_identity_snapshot,
    project_active_us_equity_assets,
)
from .alpaca import AUTH_ENVIRONMENT_TOKEN
from .http import open_without_redirects
from .nasdaq import NasdaqCompletenessPolicy, parse_nasdaq_traded
from .nasdaq_bootstrap_publisher import verify_nasdaq_bootstrap_baseline_release
from .network_execution import (
    NetworkRequestPlan,
    _bind_network_response,
    assert_local_network_request,
    start_local_network_execution,
)
from .snapshots import (
    ALLOWED_RESPONSE_HEADERS,
    AsReceivedSnapshotStore,
    LandedSnapshot,
    NetworkAcquisitionRegistry,
    normalize_response_headers,
)


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = Path("config/nasdaq_identity_readiness_policy.json")
ALPACA_ASSET_PROJECTION_POLICY_PATH = Path(
    "config/alpaca_asset_projection_policy.json"
)
ALPACA_ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets"
ALPACA_ASSETS_SOURCE = "alpaca_assets"
ALPACA_ASSETS_MAX_BYTES = 32 * 1024 * 1024
ALPACA_ASSETS_TIMEOUT_SECONDS = 30
ALPACA_ASSETS_MAX_PAGES = 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(payload) is not dict:
        raise IntegrityError(f"{label} must be one JSON object")
    return payload


def _validate_policy_shape(policy: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "project",
        "authorization_plan",
        "authorization_plan_id",
        "base_tree",
        "publication_eligibility_remediation",
        "publication_eligibility_remediation_id",
        "baseline_contract",
        "alpaca_asset_projection_policy_id",
        "identity_release_contract",
        "execution_contract",
        "environment_id",
        "network_registry_id",
        "policy_id",
    }
    if set(policy) != expected:
        raise ContractError("Nasdaq identity readiness policy fields differ")
    if (
        type(policy["schema_version"]) is not int
        or policy["schema_version"] != 1
        or policy["project"] != PROJECT
    ):
        raise ContractError("Nasdaq identity readiness policy identity differs")
    for name in (
        "authorization_plan_id",
        "publication_eligibility_remediation_id",
        "environment_id",
        "network_registry_id",
        "policy_id",
    ):
        require_sha256(policy[name], f"identity readiness policy {name}")
    if (
        policy["authorization_plan_id"]
        != "c34aebff74beee7d256603880c06ae567c8faf21b86f3aadd5f519e197a5c545"
        or policy["base_tree"] != "ebf85be7b58183afdf88b99a9a6a38d60c82feb2"
    ):
        raise ContractError("identity readiness reviewed Git/plan binding differs")
    authorization = policy["authorization_plan"]
    if type(authorization) is not dict:
        raise ContractError("identity readiness authorization plan is invalid")
    if (
        authorization.get("schema_version") != 1
        or authorization.get("plan_type")
        != "NASDAQ_IDENTITY_RELEASE_READINESS_IMPLEMENTATION"
        or authorization.get("base_commit")
        != "ac5c9142172736e820427024be6ddb902cd9c177"
        or authorization.get("network_registry_id") != policy["network_registry_id"]
        or authorization.get("commit_message")
        != "Implement Nasdaq identity release readiness"
        or authorization.get("stop_after_commit") is not True
    ):
        raise ContractError("identity readiness authorization binding differs")
    if policy["authorization_plan_id"] != sha256_bytes(
        canonical_json_bytes(authorization)
    ):
        raise ContractError("identity readiness authorization plan ID differs")
    remediation = policy["publication_eligibility_remediation"]
    if remediation != {
        "schema_version": 1,
        "record_type": "IDENTITY_PUBLICATION_ELIGIBILITY_REMEDIATION",
        "base_commit": "a554f957f05fa88aa694da8f14d44749256ee0d8",
        "base_tree": "fa905e28f4788f99608c27b0fbd0d20a0692cf43",
        "preserved_authorization_plan_id": (
            "c34aebff74beee7d256603880c06ae567c8faf21b86f3aadd5f519e197a5c545"
        ),
        "input_assessment_id": (
            "f74cb03ebd303fc2863c2105326e1b69d6177a6d5c975cc5fcc6f208dea34da1"
        ),
        "alpaca_snapshot_id": (
            "b328103270f59e408ec3457266f03dfe2bf2a024cf38a3d38fd4b323cf47b91a"
        ),
        "nasdaq_snapshot_id": (
            "34494904a1a7db8408fba9e1ca233021fe06133faaa5744a2029ea3535c2a5c0"
        ),
        "required_successor_commit_count": 1,
        "require_clean_tree": True,
        "verification": [
            "targeted_identity_release_readiness_tests",
            "git_diff_check",
        ],
        "prohibitions": [
            "network_calls",
            "identity_release_publication",
            "config_sources_mutation",
            "source_activation",
            "model_or_research_execution",
            "secret_read_or_logging",
        ],
        "commit_message": "Remediate identity publication eligibility",
        "stop_after_commit": True,
    }:
        raise ContractError("identity publication eligibility remediation differs")
    if policy["publication_eligibility_remediation_id"] != sha256_bytes(
        canonical_json_bytes(remediation)
    ):
        raise ContractError(
            "identity publication eligibility remediation ID differs"
        )
    baseline = policy["baseline_contract"]
    if baseline != {
        "accepted_root": "data/vault/accepted",
        "dataset": "nasdaq_bootstrap_baseline",
        "release_id": "bae68471507697128071d04a32eff38489c599ce878b486365cf3eeb2d49d9c8",
        "receipt_id": "dc5bb207375e8a0f3e2563a8f5c0e6607fb0a174d6e6fade4ce518441eb7e787",
        "snapshot_b_id": "5551138f91ac5700c9106188ca8f1385499afbdd4adb06b2c0c5a38d6596bd7a",
        "record_count": 13064,
        "snapshot_b_retrieved_at": "2026-07-28T11:16:14.774539Z",
        "snapshot_b_file_created_at": "2026-07-28T11:01:00Z",
    }:
        raise ContractError("trusted Nasdaq baseline contract differs")
    release = policy["identity_release_contract"]
    if release != {
        "dataset": "identity",
        "source_epoch": "nasdaq_alpaca_active_us_equity_v1",
        "role": "prospective_as_received",
        "quality_state": "PASS",
        "payload_filename": "identity_snapshots.json",
        "receipt_filename": "identity_publication_receipt.json",
        "accepted_root": "data/vault/accepted",
        "work_root": "data/w/nasdaq_identity",
        "publication_count": 1,
    }:
        raise ContractError("identity release contract differs")
    execution = policy["execution_contract"]
    if execution != {
        "default_mode": "PLAN_ONLY_NO_WRITES",
        "alpaca_assets_execute_flag": "--execute-network",
        "identity_publish_execute_flag": "--execute",
        "network_calls_during_implementation": 0,
        "identity_releases_during_implementation": 0,
        "source_config_mutations": 0,
        "activation": False,
        "require_fresh_nasdaq_after_baseline": True,
        "require_fresh_alpaca_assets_after_baseline": True,
        "require_clean_one_commit_successor": True,
        "require_production_system_utc": True,
        "atomic_content_addressed_publication": True,
        "idempotent_same_release_only": True,
    }:
        raise ContractError("identity readiness execution boundary differs")


def load_alpaca_asset_projection_policy(
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = root / ALPACA_ASSET_PROJECTION_POLICY_PATH
    require_contained_path(path, root)
    policy = _json_object(path, label="Alpaca asset projection policy")
    if set(policy) != {
        "schema_version",
        "project",
        "projection_contract",
        "projection_contract_id",
        "reviewed_evidence",
        "implementation_plan_id",
        "policy_file_id",
    }:
        raise ContractError("Alpaca asset projection policy fields differ")
    if (
        type(policy["schema_version"]) is not int
        or policy["schema_version"] != 1
        or policy["project"] != PROJECT
        or policy["implementation_plan_id"]
        != "dbb5232245c3a931a6db2376431e644960bdf64361ef7010c98e5292c01edb5c"
    ):
        raise ContractError("Alpaca asset projection policy identity differs")
    for name in (
        "projection_contract_id",
        "implementation_plan_id",
        "policy_file_id",
    ):
        require_sha256(policy[name], f"Alpaca projection policy {name}")
    if (
        policy["projection_contract_id"]
        != "e6ccdc128a73bc44a8ebdc98a0dcb53d4a5dd4e5bbc236c881fcae89c6ceff68"
        or policy["projection_contract_id"]
        != sha256_bytes(canonical_json_bytes(policy["projection_contract"]))
    ):
        raise ContractError("Alpaca asset projection contract ID differs")
    reviewed = policy["reviewed_evidence"]
    if reviewed != {
        "snapshot_id": "b328103270f59e408ec3457266f03dfe2bf2a024cf38a3d38fd4b323cf47b91a",
        "raw_sha256": "72f81af8eebd337bec1466ea28dcc0c67142be272d714d60f4ddebf4aabc3657",
        "retrieved_at": "2026-07-28T12:32:14.716418Z",
        "raw_record_count": 33379,
        "selected_record_count": 14096,
        "selected_rows_sha256": "38edf565af6808d789e87abc385e2f526f0f7d416af262ae7306827c4fcc6f96",
        "excluded_counts": {
            "crypto_active": 73,
            "us_equity_inactive": 19210,
        },
        "selected_duplicate_id_keys": 0,
        "selected_duplicate_symbol_keys": 0,
        "projection_assessment_id": "0c6469bd91e8316e16827ef38c1ee160f04942de6da210c724e6a323313d2eb3",
    }:
        raise ContractError("reviewed Alpaca projection evidence differs")
    unsigned = {
        key: value for key, value in policy.items() if key != "policy_file_id"
    }
    if policy["policy_file_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise ContractError("Alpaca asset projection policy file ID differs")
    return policy


def load_identity_readiness_policy(
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = root / POLICY_PATH
    require_contained_path(path, root)
    policy = _json_object(path, label="Nasdaq identity readiness policy")
    _validate_policy_shape(policy)
    unsigned = {key: value for key, value in policy.items() if key != "policy_id"}
    if policy["policy_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise ContractError("Nasdaq identity readiness policy ID differs")
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root / "config",
    )
    if registry.registry_id != policy["network_registry_id"]:
        raise ContractError("identity readiness network registry differs")
    if (
        validate_environment_lock(root / "config" / "environment.lock.json")
        != policy["environment_id"]
    ):
        raise ContractError("identity readiness environment differs")
    projection_policy = load_alpaca_asset_projection_policy(root)
    if (
        projection_policy["projection_contract_id"]
        != policy["alpaca_asset_projection_policy_id"]
    ):
        raise ContractError("identity readiness projection policy differs")
    return policy


@dataclass(frozen=True)
class TrustedNasdaqBaseline:
    release_id: str
    receipt_id: str
    snapshot_id: str
    record_count: int
    retrieved_at: datetime
    file_created_at: datetime


def load_trusted_nasdaq_baseline(
    *,
    repo_root: Path | None = None,
    accepted_root: Path | None = None,
) -> TrustedNasdaqBaseline:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    policy = load_identity_readiness_policy(root)
    contract = policy["baseline_contract"]
    accepted = Path(accepted_root or root / contract["accepted_root"])
    expected_accepted = root / "data" / "vault" / "accepted"
    if accepted != expected_accepted:
        raise ContractError("Nasdaq baseline accepted root differs")
    release_directory = accepted / contract["dataset"] / contract["release_id"]
    receipt = verify_nasdaq_bootstrap_baseline_release(
        release_directory,
        accepted_root=accepted,
    )
    baseline = receipt.get("baseline")
    snapshot_b = receipt.get("snapshot_b")
    if (
        receipt.get("receipt_id") != contract["receipt_id"]
        or receipt.get("status") != "PASS_BOOTSTRAP_BASELINE_PUBLISHED_NOT_ACTIVE"
        or type(baseline) is not dict
        or baseline.get("continuity_baseline_eligible") is not True
        or baseline.get("source_active") is not False
        or baseline.get("record_count") != contract["record_count"]
        or baseline.get("snapshot_id") != contract["snapshot_b_id"]
        or type(snapshot_b) is not dict
        or snapshot_b.get("snapshot_id") != contract["snapshot_b_id"]
        or snapshot_b.get("record_count") != contract["record_count"]
        or snapshot_b.get("retrieved_at")
        != contract["snapshot_b_retrieved_at"]
        or snapshot_b.get("file_created_at")
        != contract["snapshot_b_file_created_at"]
    ):
        raise IntegrityError("published Nasdaq continuity baseline differs")
    return TrustedNasdaqBaseline(
        release_id=contract["release_id"],
        receipt_id=contract["receipt_id"],
        snapshot_id=contract["snapshot_b_id"],
        record_count=contract["record_count"],
        retrieved_at=parse_utc_z(
            contract["snapshot_b_retrieved_at"],
            "baseline.snapshot_b_retrieved_at",
        ),
        file_created_at=parse_utc_z(
            contract["snapshot_b_file_created_at"],
            "baseline.snapshot_b_file_created_at",
        ),
    )


def build_alpaca_assets_request_plan(
    repo_root: Path | None = None,
) -> NetworkRequestPlan:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    policy = load_identity_readiness_policy(root)
    request = policy["authorization_plan"]["alpaca_assets_future_capture_contract"]
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root / "config",
    )
    return NetworkRequestPlan.create(
        registry=registry,
        source=ALPACA_ASSETS_SOURCE,
        initial_url=request["url"],
        timeout_seconds=request["timeout_seconds"],
        max_response_bytes=request["max_response_bytes"],
        max_pages=request["max_pages"],
        pagination_parameter=None,
    )


def guarded_capture_alpaca_assets(
    *,
    approved_plan_id: str,
    snapshot_store: AsReceivedSnapshotStore,
    api_key_id: str,
    api_secret_key: str,
    clock: TrustedClock,
    repo_root: Path | None = None,
    network_enabled: bool = False,
) -> LandedSnapshot:
    if not network_enabled or os.environ.get(AUTH_ENVIRONMENT_TOKEN) != "YES":
        raise NetworkGuardError(
            f"network disabled; require explicit flag and {AUTH_ENVIRONMENT_TOKEN}=YES"
        )
    if not api_key_id or not api_secret_key:
        raise ContractError("Alpaca credentials must be supplied from the environment")
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise ContractError("Alpaca asset capture requires production system UTC")
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root / "config",
    )
    plan = build_alpaca_assets_request_plan(root)
    require_sha256(approved_plan_id, "approved Alpaca asset request plan ID")
    if approved_plan_id != plan.plan_id:
        raise PermissionError("approved Alpaca asset request plan ID differs")
    if snapshot_store.acquisition_registry is None:
        raise ContractError("Alpaca asset capture requires the pinned network registry")
    if snapshot_store.root != (
        root / "data" / "vault" / "qualification" / "as_received"
    ) or snapshot_store.allowed_root != root:
        raise ContractError("Alpaca asset capture snapshot root differs")
    if snapshot_store.acquisition_registry.registry_id != registry.registry_id:
        raise ContractError("Alpaca asset snapshot store registry differs")
    session = start_local_network_execution(
        plan,
        registry=registry,
        clock=trusted_clock,
    )
    attempt = assert_local_network_request(
        session,
        source=ALPACA_ASSETS_SOURCE,
        url=ALPACA_ASSETS_URL,
        timeout_seconds=ALPACA_ASSETS_TIMEOUT_SECONDS,
        max_response_bytes=ALPACA_ASSETS_MAX_BYTES,
        page_index=0,
        expected_page_token=None,
        clock=trusted_clock,
    )
    request = Request(
        ALPACA_ASSETS_URL,
        headers={
            "APCA-API-KEY-ID": api_key_id,
            "APCA-API-SECRET-KEY": api_secret_key,
        },
        method="GET",
    )
    try:
        with open_without_redirects(
            request,
            timeout_seconds=ALPACA_ASSETS_TIMEOUT_SECONDS,
        ) as response:
            raw = response.read(ALPACA_ASSETS_MAX_BYTES + 1)
            headers = normalize_response_headers(
                {
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in ALLOWED_RESPONSE_HEADERS
                }
            )
            status = int(response.status)
            response_url = str(response.geturl())
    except HTTPError as response:
        raw = response.read(ALPACA_ASSETS_MAX_BYTES + 1)
        headers = normalize_response_headers(
            {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in ALLOWED_RESPONSE_HEADERS
            }
        )
        status = int(response.code)
        response_url = str(response.geturl())
    if response_url != ALPACA_ASSETS_URL:
        raise ContractError("Alpaca asset response redirected away from the approved URL")
    if len(raw) > ALPACA_ASSETS_MAX_BYTES:
        raise ContractError("Alpaca asset response exceeded the bounded byte limit")
    evidence = _bind_network_response(
        attempt,
        requested_url=ALPACA_ASSETS_URL,
        response_url=response_url,
        http_status=status,
        raw=raw,
        headers=headers,
    )
    if status != 200:
        raise ContractError("Alpaca asset response HTTP status is not accepted")
    snapshot = snapshot_store._land_network_response(
        transport_evidence=evidence,
        source=ALPACA_ASSETS_SOURCE,
        requested_url=ALPACA_ASSETS_URL,
        response_url=response_url,
        http_status=status,
        raw=raw,
        headers=headers,
        clock=trusted_clock,
        max_bytes=ALPACA_ASSETS_MAX_BYTES,
    )
    projection_policy = load_alpaca_asset_projection_policy(root)
    projection = project_active_us_equity_assets(
        snapshot,
        projection_contract=projection_policy["projection_contract"],
        projection_contract_id=projection_policy["projection_contract_id"],
    )
    if not projection.records or not projection.trust_eligible:
        raise IntegrityError("Alpaca asset capture is empty or not trust eligible")
    return snapshot


@dataclass(frozen=True)
class IdentityInputAssessment:
    assessment_id: str
    baseline: TrustedNasdaqBaseline
    alpaca_snapshot_id: str
    alpaca_raw_sha256: str
    alpaca_receipt_sha256: str
    alpaca_record_count: int
    alpaca_raw_record_count: int
    alpaca_projection_contract_id: str
    alpaca_projection_assessment_id: str
    alpaca_selected_rows_sha256: str
    alpaca_excluded_counts: tuple[tuple[str, int], ...]
    nasdaq_snapshot_id: str
    nasdaq_raw_sha256: str
    nasdaq_receipt_sha256: str
    nasdaq_record_count: int
    identity_snapshot: IdentitySnapshot

    def summary(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "status": (
                "PASS_IDENTITY_INPUTS_READY_NOT_PUBLISHED_NOT_ACTIVE"
                if self.identity_snapshot.trust_eligible
                else "SYNTHETIC_IDENTITY_MECHANICS_ONLY"
            ),
            "baseline_release_id": self.baseline.release_id,
            "baseline_receipt_id": self.baseline.receipt_id,
            "baseline_snapshot_id": self.baseline.snapshot_id,
            "baseline_record_count": self.baseline.record_count,
            "alpaca_snapshot_id": self.alpaca_snapshot_id,
            "alpaca_raw_sha256": self.alpaca_raw_sha256,
            "alpaca_receipt_sha256": self.alpaca_receipt_sha256,
            "alpaca_record_count": self.alpaca_record_count,
            "alpaca_raw_record_count": self.alpaca_raw_record_count,
            "alpaca_projection_contract_id": self.alpaca_projection_contract_id,
            "alpaca_projection_assessment_id": (
                self.alpaca_projection_assessment_id
            ),
            "alpaca_selected_rows_sha256": self.alpaca_selected_rows_sha256,
            "alpaca_excluded_counts": dict(self.alpaca_excluded_counts),
            "nasdaq_snapshot_id": self.nasdaq_snapshot_id,
            "nasdaq_raw_sha256": self.nasdaq_raw_sha256,
            "nasdaq_receipt_sha256": self.nasdaq_receipt_sha256,
            "nasdaq_record_count": self.nasdaq_record_count,
            "identity_snapshot_id": self.identity_snapshot.snapshot_id,
            "identity_row_count": len(self.identity_snapshot.rows),
            "effective_at": iso_z(self.identity_snapshot.effective_at),
            "known_at": iso_z(self.identity_snapshot.known_at),
            "evidence_state": self.identity_snapshot.evidence_state,
            "identity_release_publication": False,
            "source_activation": False,
        }


def _assess_loaded_inputs(
    *,
    alpaca_snapshot: LandedSnapshot,
    nasdaq_snapshot: LandedSnapshot,
    baseline: TrustedNasdaqBaseline,
    nasdaq_policy: NasdaqCompletenessPolicy | None,
    alpaca_projection_policy: Mapping[str, Any],
    require_production: bool,
) -> IdentityInputAssessment:
    if (
        alpaca_snapshot.source != ALPACA_ASSETS_SOURCE
        or alpaca_snapshot.url != ALPACA_ASSETS_URL
    ):
        raise ContractError("Alpaca asset snapshot differs from the frozen source")
    if nasdaq_snapshot.source != "nasdaqtraded":
        raise ContractError("Nasdaq identity snapshot differs from the frozen source")
    if require_production and (
        not alpaca_snapshot.trust_eligible or not nasdaq_snapshot.trust_eligible
    ):
        raise ContractError("production identity assessment requires network snapshots")
    if alpaca_snapshot.retrieved_at <= baseline.retrieved_at:
        raise ContractError("Alpaca asset snapshot is not newer than the trusted baseline")
    if (
        nasdaq_snapshot.snapshot_id == baseline.snapshot_id
        or nasdaq_snapshot.retrieved_at <= baseline.retrieved_at
    ):
        raise ContractError("Nasdaq identity snapshot is not newer than snapshot B")
    projection = project_active_us_equity_assets(
        alpaca_snapshot,
        projection_contract=alpaca_projection_policy["projection_contract"],
        projection_contract_id=alpaca_projection_policy[
            "projection_contract_id"
        ],
    )
    assets = projection.records
    listings = parse_nasdaq_traded(
        nasdaq_snapshot,
        policy=nasdaq_policy,
        prior_accepted_record_count=baseline.record_count,
    )
    file_times = {record.file_created_at for record in listings}
    if len(file_times) != 1 or next(iter(file_times)) <= baseline.file_created_at:
        raise ContractError(
            "Nasdaq identity snapshot file-creation time is not newer than snapshot B"
        )
    merged = merge_identity_snapshot(assets, listings)
    if require_production and not merged.trust_eligible:
        raise ContractError("merged production identity snapshot is not trust eligible")
    unsigned = {
        "schema_version": 1,
        "baseline_release_id": baseline.release_id,
        "baseline_receipt_id": baseline.receipt_id,
        "baseline_snapshot_id": baseline.snapshot_id,
        "baseline_record_count": baseline.record_count,
        "alpaca_snapshot_id": alpaca_snapshot.snapshot_id,
        "alpaca_raw_sha256": alpaca_snapshot.raw_sha256,
        "alpaca_receipt_sha256": sha256_file(alpaca_snapshot.root / "receipt.json"),
        "alpaca_record_count": len(assets),
        "alpaca_raw_record_count": projection.raw_record_count,
        "alpaca_projection_contract_id": projection.projection_contract_id,
        "alpaca_projection_assessment_id": projection.projection_assessment_id,
        "alpaca_selected_rows_sha256": projection.selected_rows_sha256,
        "alpaca_excluded_counts": dict(projection.excluded_counts),
        "nasdaq_snapshot_id": nasdaq_snapshot.snapshot_id,
        "nasdaq_raw_sha256": nasdaq_snapshot.raw_sha256,
        "nasdaq_receipt_sha256": sha256_file(nasdaq_snapshot.root / "receipt.json"),
        "nasdaq_record_count": len(listings),
        "identity_snapshot_id": merged.snapshot_id,
        "identity_row_count": len(merged.rows),
        "effective_at": iso_z(merged.effective_at),
        "known_at": iso_z(merged.known_at),
        "evidence_state": merged.evidence_state,
    }
    return IdentityInputAssessment(
        assessment_id=sha256_bytes(canonical_json_bytes(unsigned)),
        baseline=baseline,
        alpaca_snapshot_id=alpaca_snapshot.snapshot_id,
        alpaca_raw_sha256=alpaca_snapshot.raw_sha256,
        alpaca_receipt_sha256=unsigned["alpaca_receipt_sha256"],
        alpaca_record_count=len(assets),
        alpaca_raw_record_count=projection.raw_record_count,
        alpaca_projection_contract_id=projection.projection_contract_id,
        alpaca_projection_assessment_id=projection.projection_assessment_id,
        alpaca_selected_rows_sha256=projection.selected_rows_sha256,
        alpaca_excluded_counts=projection.excluded_counts,
        nasdaq_snapshot_id=nasdaq_snapshot.snapshot_id,
        nasdaq_raw_sha256=nasdaq_snapshot.raw_sha256,
        nasdaq_receipt_sha256=unsigned["nasdaq_receipt_sha256"],
        nasdaq_record_count=len(listings),
        identity_snapshot=merged,
    )


def assess_identity_inputs(
    *,
    alpaca_snapshot_directory: Path,
    nasdaq_snapshot_directory: Path,
    repo_root: Path | None = None,
    accepted_root: Path | None = None,
) -> IdentityInputAssessment:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    policy = load_identity_readiness_policy(root)
    source_config = _json_object(
        root / "config" / "sources.json",
        label="source configuration",
    )
    expected_store = root / "data" / "vault" / "qualification" / "as_received"
    if (
        source_config.get("project") != PROJECT
        or Path(str(source_config.get("snapshot_store_root"))) != expected_store
    ):
        raise ContractError("identity snapshot store differs from source configuration")
    sources = source_config.get("sources")
    if type(sources) is not dict:
        raise ContractError("source configuration is invalid")
    nasdaq_source = sources.get("nasdaq_symbol_directory")
    if (
        type(nasdaq_source) is not dict
        or nasdaq_source.get("enabled_for_active_pipeline") is not False
        or nasdaq_source.get("qualification_receipt")
        != "config/nasdaq_qualification_receipt.json"
    ):
        raise ContractError("Nasdaq source must remain preserved and inactive")
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root / "config",
    )
    if registry.registry_id != policy["network_registry_id"]:
        raise ContractError("identity assessment registry differs")
    store = AsReceivedSnapshotStore(
        expected_store,
        allowed_root=root,
        acquisition_registry=registry,
    )
    alpaca = store.load(Path(alpaca_snapshot_directory))
    nasdaq = store.load(Path(nasdaq_snapshot_directory))
    baseline = load_trusted_nasdaq_baseline(
        repo_root=root,
        accepted_root=accepted_root,
    )
    return _assess_loaded_inputs(
        alpaca_snapshot=alpaca,
        nasdaq_snapshot=nasdaq,
        baseline=baseline,
        nasdaq_policy=None,
        alpaca_projection_policy=load_alpaca_asset_projection_policy(root),
        require_production=True,
    )


def verify_alpaca_asset_snapshot(
    *,
    snapshot_directory: Path,
    repo_root: Path | None = None,
) -> AlpacaAssetProjection:
    """Reverify one landed snapshot and its projection without writes."""

    root = Path(repo_root or _repo_root()).resolve(strict=True)
    source_config = _json_object(
        root / "config" / "sources.json",
        label="source configuration",
    )
    expected_store = root / "data" / "vault" / "qualification" / "as_received"
    if (
        source_config.get("project") != PROJECT
        or Path(str(source_config.get("snapshot_store_root"))) != expected_store
    ):
        raise ContractError("identity snapshot store differs from source configuration")
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root / "config",
    )
    store = AsReceivedSnapshotStore(
        expected_store,
        allowed_root=root,
        acquisition_registry=registry,
    )
    snapshot = store.load(Path(snapshot_directory))
    if not snapshot.local_integrity_verified:
        raise ContractError("Alpaca asset snapshot is not locally integrity verified")
    policy = load_alpaca_asset_projection_policy(root)
    projection = project_active_us_equity_assets(
        snapshot,
        projection_contract=policy["projection_contract"],
        projection_contract_id=policy["projection_contract_id"],
    )
    reviewed = policy["reviewed_evidence"]
    if snapshot.snapshot_id == reviewed["snapshot_id"] and (
        snapshot.raw_sha256 != reviewed["raw_sha256"]
        or iso_z(snapshot.retrieved_at) != reviewed["retrieved_at"]
        or projection.raw_record_count != reviewed["raw_record_count"]
        or projection.selected_record_count != reviewed["selected_record_count"]
        or projection.selected_rows_sha256 != reviewed["selected_rows_sha256"]
        or dict(projection.excluded_counts) != reviewed["excluded_counts"]
        or projection.projection_assessment_id
        != reviewed["projection_assessment_id"]
    ):
        raise IntegrityError("reviewed Alpaca asset projection differs")
    return projection
