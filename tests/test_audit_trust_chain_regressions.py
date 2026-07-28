from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import (
    canonical_json_bytes,
    parse_timestamp,
    require_sha256,
    sha256_bytes,
)
from us_stocks_swing_model_v2.errors import (
    ContractError,
    EvaluationAuthorizationError,
    IntegrityError,
)
from us_stocks_swing_model_v2.providers.network_execution import (
    LocalNetworkExecutionSession,
    NetworkRequestAttempt,
    NetworkRequestPlan,
    NetworkResponseEvidence,
    _bind_network_response,
    assert_local_network_request,
    start_local_network_execution,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    AsReceivedSnapshotStore,
    NetworkAcquisitionCapability,
    NetworkAcquisitionRegistry,
)
from us_stocks_swing_model_v2.releases import (
    MANIFEST_NAME,
    build_manifest,
    verify_accepted_release,
    verify_release,
)


def _registry(tmp_path: Path) -> NetworkAcquisitionRegistry:
    path = tmp_path / "network_registry.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "US_stocks_swing_model_v2",
                "status": "ACTIVE",
                "allowed_sources": {
                    "fixture": {
                        "origin_path": "https://example.invalid/data",
                        "accepted_http_statuses": [200],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return NetworkAcquisitionRegistry.load(path, allowed_root=tmp_path)


def _plan(registry: NetworkAcquisitionRegistry) -> NetworkRequestPlan:
    return NetworkRequestPlan.create(
        registry=registry,
        source="fixture",
        initial_url="https://example.invalid/data",
        timeout_seconds=30,
        max_response_bytes=1024,
        max_pages=1,
        pagination_parameter=None,
    )


def _attempt(
    registry: NetworkAcquisitionRegistry,
    *,
    clock: TrustedClock,
) -> NetworkRequestAttempt:
    plan = _plan(registry)
    session = start_local_network_execution(plan, registry=registry, clock=clock)
    return assert_local_network_request(
        session,
        source="fixture",
        url=plan.initial_url,
        timeout_seconds=30,
        max_response_bytes=1024,
        page_index=0,
        expected_page_token=None,
        clock=clock,
    )


def test_local_network_execution_is_exact_ordered_and_unforgeable(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    plan = _plan(registry)
    clock = TrustedClock.production()
    session = start_local_network_execution(plan, registry=registry, clock=clock)
    attempt = assert_local_network_request(
        session,
        source="fixture",
        url=plan.initial_url,
        timeout_seconds=30,
        max_response_bytes=1024,
        page_index=0,
        expected_page_token=None,
        clock=clock,
    )
    assert type(attempt) is NetworkRequestAttempt
    with pytest.raises(EvaluationAuthorizationError, match="reused or is out of sequence"):
        assert_local_network_request(
            session,
            source="fixture",
            url=plan.initial_url,
            timeout_seconds=30,
            max_response_bytes=1024,
            page_index=0,
            expected_page_token=None,
            clock=clock,
        )

    forged = object.__new__(LocalNetworkExecutionSession)
    object.__setattr__(forged, "plan", plan)
    object.__setattr__(forged, "session_id", session.session_id)
    object.__setattr__(forged, "started_at", session.started_at)
    with pytest.raises(EvaluationAuthorizationError, match="forged or is unavailable"):
        assert_local_network_request(
            forged,
            source="fixture",
            url=plan.initial_url,
            timeout_seconds=30,
            max_response_bytes=1024,
            page_index=0,
            expected_page_token=None,
            clock=clock,
        )


def test_network_plan_rejects_duplicate_query_keys_and_registry_drift(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(ContractError, match="query keys must be unique"):
        NetworkRequestPlan.create(
            registry=registry,
            source="fixture",
            initial_url="https://example.invalid/data?symbol=A&symbol=B",
            timeout_seconds=30,
            max_response_bytes=1024,
            max_pages=1,
            pagination_parameter=None,
        )

    Path(registry.registry_path).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "US_stocks_swing_model_v2",
                "status": "ACTIVE",
                "allowed_sources": {
                    "fixture": {
                        "origin_path": "https://example.invalid/changed",
                        "accepted_http_statuses": [200],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="changed after loading"):
        registry.validate()


def test_network_response_landing_is_atomic_single_use_and_locally_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    store = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
        acquisition_registry=registry,
    )
    clock = TrustedClock.production()
    attempt = _attempt(registry, clock=clock)
    raw = b"network fixture"
    headers = {"content-type": "application/octet-stream"}
    response = _bind_network_response(
        attempt,
        requested_url="https://example.invalid/data",
        response_url="https://example.invalid/data",
        http_status=200,
        raw=raw,
        headers=headers,
    )

    original_land = store._land

    def fail_landing(**_kwargs: object) -> object:
        raise OSError("simulated landing failure")

    monkeypatch.setattr(store, "_land", fail_landing)
    with pytest.raises(OSError, match="simulated landing failure"):
        store._land_network_response(
            transport_evidence=response,
            source="fixture",
            requested_url="https://example.invalid/data",
            response_url="https://example.invalid/data",
            http_status=200,
            raw=raw,
            headers=headers,
            clock=clock,
            max_bytes=1024,
        )
    monkeypatch.setattr(store, "_land", original_land)
    snapshot = store._land_network_response(
        transport_evidence=response,
        source="fixture",
        requested_url="https://example.invalid/data",
        response_url="https://example.invalid/data",
        http_status=200,
        raw=raw,
        headers=headers,
        clock=clock,
        max_bytes=1024,
    )
    assert snapshot.local_integrity_verified is True
    assert snapshot.read_verified_bytes() == raw
    with pytest.raises(EvaluationAuthorizationError, match="replayed"):
        store._land_network_response(
            transport_evidence=response,
            source="fixture",
            requested_url="https://example.invalid/data",
            response_url="https://example.invalid/data",
            http_status=200,
            raw=raw,
            headers=headers,
            clock=clock,
            max_bytes=1024,
        )
    snapshot.raw_path.write_bytes(raw + b"\n")
    with pytest.raises(IntegrityError, match="raw bytes differ"):
        snapshot.read_verified_bytes()


def test_zero_byte_network_response_is_bound_then_rejected_as_snapshot_evidence(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    store = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
        acquisition_registry=registry,
    )
    clock = TrustedClock.production()
    attempt = _attempt(registry, clock=clock)
    response = _bind_network_response(
        attempt,
        requested_url="https://example.invalid/data",
        response_url="https://example.invalid/data",
        http_status=200,
        raw=b"",
        headers={"content-type": "application/octet-stream"},
    )
    assert response.raw_sha256 == sha256_bytes(b"")
    with pytest.raises(ContractError, match="snapshot response is empty"):
        store._land_network_response(
            transport_evidence=response,
            source="fixture",
            requested_url="https://example.invalid/data",
            response_url="https://example.invalid/data",
            http_status=200,
            raw=b"",
            headers={"content-type": "application/octet-stream"},
            clock=clock,
            max_bytes=1024,
        )


def test_network_evidence_capabilities_cannot_be_publicly_constructed() -> None:
    with pytest.raises(TypeError):
        NetworkAcquisitionCapability()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        NetworkRequestAttempt()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        NetworkResponseEvidence()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        LocalNetworkExecutionSession()  # type: ignore[call-arg]


def test_self_consistent_unpublished_release_is_not_accepted_evidence(
    tmp_path: Path,
) -> None:
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
    (tmp_path / "accepted").mkdir()
    with pytest.raises(ContractError, match="escapes its approved root"):
        verify_accepted_release(stage, accepted_root=tmp_path / "accepted")


def test_sha256_fields_reject_uppercase_shape_only_values() -> None:
    with pytest.raises(ContractError, match="lowercase SHA-256"):
        require_sha256("A" * 64, "fixture")


@pytest.mark.parametrize("value", [None, True, 1, 1.5, {}, []])
def test_parse_timestamp_rejects_non_string_inputs_as_contract_errors(
    value: object,
) -> None:
    with pytest.raises(ContractError, match="timestamp must be exact ISO-8601 text"):
        parse_timestamp(value)  # type: ignore[arg-type]


def test_synthetic_clock_remains_not_trust_eligible() -> None:
    clock = TrustedClock.synthetic_fixed(
        datetime(2026, 7, 15, tzinfo=timezone.utc),
        permit=SyntheticOnlyPermit.create(
            fixture_id="audit-synthetic-clock",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )
    assert clock.trust_eligible is False
