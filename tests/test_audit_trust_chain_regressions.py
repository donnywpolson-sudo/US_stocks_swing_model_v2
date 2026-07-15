from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.calendar import PinnedSessionCalendar
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, require_sha256, sha256_bytes
from us_stocks_swing_model_v2.errors import (
    ContractError,
    EvaluationAuthorizationError,
)
from us_stocks_swing_model_v2.gates import build_gate_receipt
from us_stocks_swing_model_v2.governance import (
    AuthorizationAuthority,
    load_external_authority,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    AsReceivedSnapshotStore,
    NetworkAcquisitionCapability,
    NetworkAcquisitionRegistry,
    normalize_response_headers,
)
from us_stocks_swing_model_v2.releases import (
    MANIFEST_NAME,
    ReleaseManifest,
    build_manifest,
    verify_accepted_release,
    verify_release,
)
from us_stocks_swing_model_v2.trials import TrialRegistry


def _permit(fixture_id: str, scope: str) -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(fixture_id=fixture_id, scope=scope)


def test_authority_requires_loader_and_revalidates_active_registry(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        AuthorizationAuthority(  # type: ignore[call-arg]
            registry_id="0" * 64,
            key_id="forged",
            key_sha256="1" * 64,
            authorization_class="EXTERNAL_USER_AUTHORITY",
            verification_key=b"forged",
        )

    key = b"externally-held-verification-key"
    registry_path = tmp_path / "authority_registry.json"
    registry = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "status": "ACTIVE",
        "authorities": [{
            "key_id": "external-user",
            "key_sha256": sha256_bytes(key),
            "authorization_class": "EXTERNAL_USER_AUTHORITY",
        }],
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    authority = load_external_authority(
        registry_path,
        key_id="external-user",
        verification_key=key,
    )
    registry["status"] = "REVOKED"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(EvaluationAuthorizationError, match="changed after loading"):
        authority.validate()


def test_network_evidence_has_no_public_capability_or_arbitrary_byte_landing() -> None:
    assert not hasattr(NetworkAcquisitionCapability, "issue")
    assert not hasattr(AsReceivedSnapshotStore, "land_network_response")
    with pytest.raises(TypeError):
        NetworkAcquisitionCapability(  # type: ignore[call-arg]
            registry_id="0" * 64,
            registry_path="forged",
            source="nasdaqtraded",
            requested_url="https://example.invalid/data",
            approved_origin_path="https://example.invalid/data",
            response_url="https://example.invalid/data",
            http_status=200,
            raw_sha256="1" * 64,
            headers_sha256="2" * 64,
            capability_id="3" * 64,
        )
    with pytest.raises(ContractError, match="outside the evidence allowlist"):
        normalize_response_headers({"authorization": "secret"})
    with pytest.raises(ContractError, match="control characters"):
        normalize_response_headers({"etag": "safe\r\npoison"})


def test_network_registry_drift_invalidates_loaded_capability_source(tmp_path: Path) -> None:
    path = tmp_path / "network_registry.json"
    payload = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "status": "ACTIVE",
        "allowed_sources": {"fixture": "https://example.invalid/data"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = NetworkAcquisitionRegistry.load(path)
    payload["status"] = "REVOKED"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="changed after loading"):
        registry.validate()


def test_self_consistent_unpublished_release_is_not_accepted_evidence(tmp_path: Path) -> None:
    stage = tmp_path / "staging" / "bars"
    stage.mkdir(parents=True)
    (stage / "bars.bin").write_bytes(b"self-consistent-but-unpublished")
    manifest = build_manifest(
        stage,
        ["bars.bin"],
        project="US_stocks_swing_model_v2",
        dataset="bars",
        source_epoch="fixture-v1",
        role="active_historical",
        quality_state="PASS",
        created_at="2026-07-15T00:00:00Z",
        row_count=1,
        event_start="2026-07-14",
        event_end="2026-07-14",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    (stage / MANIFEST_NAME).write_bytes(canonical_json_bytes(manifest.as_dict()))
    assert verify_release(stage) == manifest
    numeric_poison = manifest.as_dict()
    numeric_poison["row_count"] = True
    with pytest.raises(ContractError, match="exact JSON integers"):
        ReleaseManifest.from_dict(numeric_poison)
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    with pytest.raises(ContractError, match="escapes its approved root"):
        verify_accepted_release(stage, accepted_root=accepted_root)


def test_sha256_fields_reject_uppercase_shape_only_values() -> None:
    with pytest.raises(ContractError, match="exact lowercase SHA-256"):
        require_sha256("A" * 64, "audit.uppercase_hash")


def test_calendar_identity_cannot_be_caller_asserted() -> None:
    permit = _permit("audit-calendar", "SYNTHETIC_SESSION_CALENDAR")
    with pytest.raises(TypeError):
        PinnedSessionCalendar(  # type: ignore[call-arg]
            release_id="0" * 64,
            sessions=(date(2026, 7, 15),),
            verification_state="VERIFIED_XNYS_RELEASE",
            verification_receipt_id="1" * 64,
        )
    with pytest.raises(ContractError, match="must equal its permit ID"):
        PinnedSessionCalendar.from_iso_dates(
            "0" * 64,
            ["2026-07-15"],
            synthetic_permit=permit,
        )


def test_local_resettable_trial_registry_cannot_claim_production(tmp_path: Path) -> None:
    accepted_root = tmp_path / "accepted"
    governance_root = tmp_path / "governance"
    accepted_root.mkdir()
    governance_root.mkdir()
    with pytest.raises(EvaluationAuthorizationError, match="external immutable registry"):
        TrialRegistry(
            governance_root / "trials.jsonl",
            governance_root / "evaluations.jsonl",
            accepted_release_root=accepted_root,
            governance_root=governance_root,
            synthetic_permit=_permit("audit-trials", "SYNTHETIC_TRIAL_REGISTRY"),
            clock=TrustedClock.production(),
        )


def test_gate_receipts_cannot_be_built_from_caller_supplied_ids() -> None:
    with pytest.raises(ContractError, match="registry-issued permit"):
        build_gate_receipt(
            trial_id="1" * 64,
            evaluation_permit_id="2" * 64,
        )
