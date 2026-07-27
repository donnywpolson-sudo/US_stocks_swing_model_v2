from __future__ import annotations

import ast
import base64
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.calendar import PinnedSessionCalendar
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
from us_stocks_swing_model_v2.gates import build_gate_receipt
import us_stocks_swing_model_v2.governance as governance_module
from us_stocks_swing_model_v2.governance import (
    AuthorizationAuthority,
    EXTERNAL_SIGNATURE_ALGORITHM,
    SignedAuthorizationReceipt,
    load_external_authority,
)
import us_stocks_swing_model_v2.providers.alpaca as alpaca_module
from us_stocks_swing_model_v2.providers.alpaca import (
    AlpacaBarsPolicy,
    AlpacaBarsRequest,
    MAX_ALPACA_RESPONSE_BYTES,
    guarded_fetch_landed_pages,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    AsReceivedSnapshotStore,
    LandedSnapshot,
    NetworkAcquisitionCapability,
    NetworkAcquisitionRegistry,
    NETWORK_ACQUISITION_ATTESTATION_SCOPE,
    SignedNetworkAcquisitionReceipt,
    network_acquisition_attestation_bindings,
    normalize_response_headers,
)
from us_stocks_swing_model_v2.providers.network_authorization import (
    NETWORK_ACQUISITION_AUTHORIZATION_SCOPE,
    AuthorizedNetworkRequestAttempt,
    AuthorizedNetworkResponse,
    NetworkAuthorizationUseStore,
    NetworkAuthorizationSession,
    NetworkRequestPlan,
    _bind_authorized_network_response,
    assemble_network_authorization_receipt,
    assert_authorized_network_request,
    network_authorization_request,
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


_RFC7515_RSA_N = (
    "ofgWCuLjybRlzo0tZWJjNiuSfb4p4fAkd_wWJcyQoTbji9k0l8W26mPddx"
    "HmfHQp-Vaw-4qPCJrcS2mJPMEzP1Pt0Bm4d4QlL-yRT-SFd2lZS-pCgNMs"
    "D1W_YpRPEwOWvG6b32690r2jZ47soMZo9wGzjb_7OMg0LOL-bSf63kpaSH"
    "SXndS5z5rexMdbBYUsLA9e-KXBdQOS-UTo7WTBEMa2R2CapHg665xsmtdV"
    "MTBQY4uDZlxvb3qCo5ZwKh9kG4LT6_I5IhlJH7aGhyxXFvUK-DWNmoudF8"
    "NAco9_h9iaGNj8q2ethFkMLs91kzk2PAcDTW9gb54h4FRWyuXpoQ"
)
_RFC7515_RSA_D = (
    "Eq5xpGnNCivDflJsRQBXHx1hdR1k6Ulwe2JZD50LpXyWPEAeP88vLNO97I"
    "jlA7_GQ5sLKMgvfTeXZx9SE-7YwVol2NXOoAJe46sui395IW_GO-pWJ1O0"
    "BkTGoVEn2bKVRUCgu-GjBVaYLU6f3l9kJfFNS3E0QbVdxzubSu3Mkqzjkn"
    "439X0M_V51gfpRLI9JYanrC4D4qAdGcopV_0ZHHzQlBjudU2QvXt4ehNYT"
    "CBr6XCLQUShb1juUO1ZdiYoFaFQT5Tw8bGUl_x_jTj3ccPDVZFD9pIuhLh"
    "BOneufuBiB4cS98l2SR_RQyGWSeWjnczT0QU91p1DhOVRuOopznQ"
)
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)


def _base64url_uint(value: str) -> int:
    return int.from_bytes(
        base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4))),
        "big",
    )


def _external_public_jwk() -> bytes:
    return canonical_json_bytes(
        {
            "alg": "RS256",
            "e": "AQAB",
            "kty": "RSA",
            "n": _RFC7515_RSA_N,
            "use": "sig",
        }
    )


def _rsa_sign_for_fixture(message: bytes) -> str:
    modulus = _base64url_uint(_RFC7515_RSA_N)
    private_exponent = _base64url_uint(_RFC7515_RSA_D)
    width = (modulus.bit_length() + 7) // 8
    digest_info = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    encoded = (
        b"\x00\x01"
        + (b"\xff" * (width - len(digest_info) - 3))
        + b"\x00"
        + digest_info
    )
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus)
    return signature.to_bytes(width, "big").hex()


def _external_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AuthorizationAuthority, Path, dict[str, object]]:
    public_jwk = _external_public_jwk()
    registry_path = tmp_path / "authority_registry.json"
    registry: dict[str, object] = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "status": "ACTIVE",
        "authorities": [{
            "key_id": "RFC7515-TEST-FIXTURE-ONLY",
            "key_sha256": sha256_bytes(public_jwk),
            "authorization_class": "EXTERNAL_USER_AUTHORITY",
            "signature_algorithm": EXTERNAL_SIGNATURE_ALGORITHM,
        }],
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(
        governance_module,
        "_reviewed_external_authority_registry_path",
        lambda: registry_path,
    )
    authority = load_external_authority(
        registry_path,
        key_id="RFC7515-TEST-FIXTURE-ONLY",
        verification_key=public_jwk,
    )
    return authority, registry_path, registry


def _network_authorization_receipt(
    plan: NetworkRequestPlan,
    *,
    registry: NetworkAcquisitionRegistry,
    authority: AuthorizationAuthority,
    clock: TrustedClock,
    lifetime: timedelta = timedelta(minutes=10),
) -> SignedAuthorizationReceipt:
    issued = clock.now()
    request = dict(
        network_authorization_request(
            plan,
            registry=registry,
            clock=clock,
            nonce="A" * 43,
        )
    )
    request["expires_at"] = (
        issued + lifetime
    ).isoformat().replace("+00:00", "Z")
    signing = {
        **request,
        "key_id": authority.key_id,
        "authority_registry_id": authority.registry_id,
        "authorization_class": authority.authorization_class,
    }
    signature = _rsa_sign_for_fixture(canonical_json_bytes(signing))
    return assemble_network_authorization_receipt(
        request,
        signature_hex=signature,
        authority=authority,
        clock=clock,
    )


def test_network_authorization_is_exact_and_single_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "network_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "US_stocks_swing_model_v2",
                "status": "ACTIVE",
                "allowed_sources": {
                    "nasdaqtraded": {
                        "origin_path": (
                            "https://www.nasdaqtrader.com/dynamic/SymDir/"
                            "nasdaqtraded.txt"
                        ),
                        "accepted_http_statuses": [200],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = NetworkAcquisitionRegistry.load(
        registry_path,
        allowed_root=tmp_path,
    )
    plan = NetworkRequestPlan.create(
        registry=registry,
        source="nasdaqtraded",
        initial_url=(
            "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
        ),
        timeout_seconds=30,
        max_response_bytes=32 * 1024 * 1024,
        max_pages=1,
        pagination_parameter=None,
    )
    authority, _, _ = _external_authority(tmp_path, monkeypatch)
    at = datetime(2026, 7, 15, 20, tzinfo=timezone.utc)
    clock = TrustedClock.synthetic_fixed(
        at,
        permit=_permit("network-authorization-clock", "TRUSTED_CLOCK_FIXED_TIME"),
    )
    receipt = _network_authorization_receipt(
        plan,
        registry=registry,
        authority=authority,
        clock=clock,
    )
    store = NetworkAuthorizationUseStore(
        tmp_path / "authorization-uses",
        allowed_root=tmp_path,
        registry=registry,
    )
    session = store.authorize(
        plan=plan,
        receipt=receipt,
        authority=authority,
        clock=clock,
    )
    request_attempt = assert_authorized_network_request(
        session,
        source="nasdaqtraded",
        url=plan.initial_url,
        timeout_seconds=30,
        max_response_bytes=32 * 1024 * 1024,
        page_index=0,
        expected_page_token=None,
        clock=clock,
    )
    assert type(request_attempt) is AuthorizedNetworkRequestAttempt
    # Authorization is for one attempt, not one successful response. A
    # transport failure after this pre-request check must burn the page index.
    with pytest.raises(TimeoutError):
        raise TimeoutError("simulated transport interruption")
    with pytest.raises(EvaluationAuthorizationError, match="reused or is out of sequence"):
        assert_authorized_network_request(
            session,
            source="nasdaqtraded",
            url=plan.initial_url,
            timeout_seconds=30,
            max_response_bytes=32 * 1024 * 1024,
            page_index=0,
            expected_page_token=None,
            clock=clock,
        )
    expired_clock = TrustedClock.synthetic_fixed(
        at + timedelta(minutes=10),
        permit=_permit(
            "network-authorization-expired-clock",
            "TRUSTED_CLOCK_FIXED_TIME",
        ),
    )
    with pytest.raises(EvaluationAuthorizationError, match="has expired"):
        assert_authorized_network_request(
            session,
            source="nasdaqtraded",
            url=plan.initial_url,
            timeout_seconds=30,
            max_response_bytes=32 * 1024 * 1024,
            page_index=1,
            expected_page_token=None,
            clock=expired_clock,
        )
    with pytest.raises(EvaluationAuthorizationError, match="already been consumed"):
        store.authorize(
            plan=plan,
            receipt=receipt,
            authority=authority,
            clock=clock,
        )
    with pytest.raises(EvaluationAuthorizationError, match="reused or is out of sequence"):
        assert_authorized_network_request(
            session,
            source="nasdaqtraded",
            url=plan.initial_url + "?changed=true",
            timeout_seconds=30,
            max_response_bytes=32 * 1024 * 1024,
            page_index=0,
            expected_page_token=None,
            clock=clock,
        )
    forged = object.__new__(NetworkAuthorizationSession)
    object.__setattr__(forged, "plan", plan)
    object.__setattr__(forged, "receipt_id", receipt.receipt_id)
    object.__setattr__(forged, "nonce", "A" * 43)
    object.__setattr__(forged, "consumed_at", clock.now().isoformat())
    object.__setattr__(forged, "expires_at", receipt.expires_at)
    with pytest.raises(EvaluationAuthorizationError, match="not issued"):
        assert_authorized_network_request(
            forged,
            source="nasdaqtraded",
            url=plan.initial_url,
            timeout_seconds=30,
            max_response_bytes=32 * 1024 * 1024,
            page_index=0,
            expected_page_token=None,
            clock=clock,
        )


def test_network_request_plan_is_revalidated_against_registry_at_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "network_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "US_stocks_swing_model_v2",
                "status": "ACTIVE",
                "allowed_sources": {
                    "fixture": {
                        "origin_path": "https://example.com/data",
                        "accepted_http_statuses": [200],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = NetworkAcquisitionRegistry.load(
        registry_path,
        allowed_root=tmp_path,
    )
    plan = NetworkRequestPlan.create(
        registry=registry,
        source="fixture",
        initial_url="https://example.com/data?window=bounded",
        timeout_seconds=30,
        max_response_bytes=1024,
        max_pages=1,
        pagination_parameter=None,
    )
    authority, _, _ = _external_authority(tmp_path, monkeypatch)
    clock = TrustedClock.synthetic_fixed(
        datetime(2026, 7, 15, 20, tzinfo=timezone.utc),
        permit=_permit("network-plan-validation-clock", "TRUSTED_CLOCK_FIXED_TIME"),
    )
    receipt = _network_authorization_receipt(
        plan,
        registry=registry,
        authority=authority,
        clock=clock,
    )
    store = NetworkAuthorizationUseStore(
        tmp_path / "authorization-uses",
        allowed_root=tmp_path,
        registry=registry,
    )

    unsigned = {
        **plan._unsigned(),
        "initial_url": "https://attacker.invalid/data",
    }
    forged_origin = NetworkRequestPlan(
        **unsigned,
        plan_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    forged_variants = (
        replace(plan, max_response_bytes=2048),
        replace(plan, plan_id="f" * 64),
        replace(plan, network_registry_id="e" * 64),
        forged_origin,
    )
    for forged in forged_variants:
        with pytest.raises(
            EvaluationAuthorizationError,
            match="not registry-bound",
        ):
            network_authorization_request(
                forged,
                registry=registry,
                clock=clock,
            )
        with pytest.raises(
            EvaluationAuthorizationError,
            match="not registry-bound",
        ):
            store.authorize(
                plan=forged,
                receipt=receipt,
                authority=authority,
                clock=clock,
            )

    session = store.authorize(
        plan=plan,
        receipt=receipt,
        authority=authority,
        clock=clock,
    )
    object.__setattr__(session.plan, "max_response_bytes", 2048)
    with pytest.raises(
        EvaluationAuthorizationError,
        match="session plan is invalid",
    ):
        assert_authorized_network_request(
            session,
            source="fixture",
            url=plan.initial_url,
            timeout_seconds=30,
            max_response_bytes=2048,
            page_index=0,
            expected_page_token=None,
            clock=clock,
        )


def test_network_authorization_rejects_duplicate_query_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "network_registry.json"
    registry_path.write_text(
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
    registry = NetworkAcquisitionRegistry.load(
        registry_path,
        allowed_root=tmp_path,
    )
    with pytest.raises(ContractError, match="query keys must be unique"):
        NetworkRequestPlan.create(
            registry=registry,
            source="fixture",
            initial_url="https://example.invalid/data?symbol=ABC&symbol=XYZ",
            timeout_seconds=30,
            max_response_bytes=1024,
            max_pages=2,
            pagination_parameter="page_token",
        )

    plan = NetworkRequestPlan.create(
        registry=registry,
        source="fixture",
        initial_url="https://example.invalid/data?symbol=ABC",
        timeout_seconds=30,
        max_response_bytes=1024,
        max_pages=2,
        pagination_parameter="page_token",
    )
    authority, _, _ = _external_authority(tmp_path, monkeypatch)
    clock = TrustedClock.synthetic_fixed(
        datetime(2026, 7, 15, 20, tzinfo=timezone.utc),
        permit=_permit(
            "duplicate-query-clock",
            "TRUSTED_CLOCK_FIXED_TIME",
        ),
    )
    receipt = _network_authorization_receipt(
        plan,
        registry=registry,
        authority=authority,
        clock=clock,
    )
    session = NetworkAuthorizationUseStore(
        tmp_path / "uses",
        allowed_root=tmp_path,
        registry=registry,
    ).authorize(
        plan=plan,
        receipt=receipt,
        authority=authority,
        clock=clock,
    )
    assert_authorized_network_request(
        session,
        source="fixture",
        url=plan.initial_url,
        timeout_seconds=30,
        max_response_bytes=1024,
        page_index=0,
        expected_page_token=None,
        clock=clock,
    )
    with pytest.raises(
        EvaluationAuthorizationError,
        match="query keys must be unique",
    ):
        assert_authorized_network_request(
            session,
            source="fixture",
            url=(
                "https://example.invalid/data?"
                "symbol=ABC&symbol=XYZ&page_token=next"
            ),
            timeout_seconds=30,
            max_response_bytes=1024,
            page_index=1,
            expected_page_token="next",
            clock=clock,
        )


def test_network_authorization_rejects_excessive_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "network_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "US_stocks_swing_model_v2",
                "status": "ACTIVE",
                "allowed_sources": {
                    "fixture": {
                        "origin_path": "https://example.com/data",
                        "accepted_http_statuses": [200],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    plan = NetworkRequestPlan.create(
        registry=NetworkAcquisitionRegistry.load(
            registry_path,
            allowed_root=tmp_path,
        ),
        source="fixture",
        initial_url="https://example.com/data",
        timeout_seconds=30,
        max_response_bytes=1024,
        max_pages=1,
        pagination_parameter=None,
    )
    authority, _, _ = _external_authority(tmp_path, monkeypatch)
    at = datetime(2026, 7, 15, 20, tzinfo=timezone.utc)
    clock = TrustedClock.synthetic_fixed(
        at,
        permit=_permit("network-lifetime-clock", "TRUSTED_CLOCK_FIXED_TIME"),
    )
    with pytest.raises(EvaluationAuthorizationError, match="ten minutes"):
        _network_authorization_receipt(
            plan=plan,
            registry=NetworkAcquisitionRegistry.load(
                registry_path,
                allowed_root=tmp_path,
            ),
            authority=authority,
            clock=clock,
            lifetime=timedelta(minutes=11),
        )


def test_external_authority_verifies_asymmetric_receipts_and_revalidates_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TypeError):
        AuthorizationAuthority(  # type: ignore[call-arg]
            registry_id="0" * 64,
            key_id="forged",
            key_sha256="1" * 64,
            authorization_class="EXTERNAL_USER_AUTHORITY",
            signature_algorithm=EXTERNAL_SIGNATURE_ALGORITHM,
            verification_key=b"forged",
        )

    authority, registry_path, registry = _external_authority(
        tmp_path,
        monkeypatch,
    )
    signing = {
        "schema_version": 1,
        "scope": "AUTHORIZE_FIXTURE",
        "subject_id": "1" * 64,
        "bindings": {"evidence": "2" * 64},
        "issued_at": "2026-07-15T19:00:00Z",
        "expires_at": "2026-07-15T21:00:00Z",
        "key_id": authority.key_id,
        "authority_registry_id": authority.registry_id,
        "authorization_class": authority.authorization_class,
    }
    signature = _rsa_sign_for_fixture(canonical_json_bytes(signing))
    unsigned = {**signing, "signature": signature}
    receipt = SignedAuthorizationReceipt(
        **signing,
        signature=signature,
        receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    receipt.validate_at(
        authority=authority,
        expected_scope="AUTHORIZE_FIXTURE",
        expected_subject_id="1" * 64,
        required_bindings={"evidence": "2" * 64},
        observed_at=datetime(2026, 7, 15, 20, tzinfo=timezone.utc),
    )
    poisoned_payload = receipt.as_dict()
    poisoned_payload["bindings"] = {"evidence": True}
    with pytest.raises(
        EvaluationAuthorizationError,
        match="exact string pairs",
    ):
        SignedAuthorizationReceipt.from_dict(poisoned_payload)
    tampered = replace(
        receipt,
        signature=receipt.signature[:-1] + (
            "0" if receipt.signature[-1] != "0" else "1"
        ),
    )
    tampered = replace(
        tampered,
        receipt_id=sha256_bytes(canonical_json_bytes(tampered.unsigned_dict())),
    )
    with pytest.raises(EvaluationAuthorizationError, match="signature is invalid"):
        tampered.validate_at(
            authority=authority,
            expected_scope="AUTHORIZE_FIXTURE",
            expected_subject_id="1" * 64,
            required_bindings={"evidence": "2" * 64},
            observed_at=datetime(2026, 7, 15, 20, tzinfo=timezone.utc),
        )
    registry["status"] = "REVOKED"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(EvaluationAuthorizationError, match="changed after loading"):
        authority.validate()


def test_network_evidence_has_no_public_capability_or_arbitrary_byte_landing() -> None:
    assert not hasattr(NetworkAcquisitionCapability, "issue")
    assert not hasattr(AsReceivedSnapshotStore, "land_network_response")
    with pytest.raises(TypeError):
        LandedSnapshot()  # type: ignore[call-arg]
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
    with pytest.raises(TypeError):
        AuthorizedNetworkRequestAttempt()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AuthorizedNetworkResponse()  # type: ignore[call-arg]
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
        "allowed_sources": {
            "fixture": {
                "origin_path": "https://example.invalid/data",
                "accepted_http_statuses": [200],
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = NetworkAcquisitionRegistry.load(path, allowed_root=tmp_path)
    payload["status"] = "REVOKED"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="changed after loading"):
        registry.validate()


def test_network_registry_operative_policy_is_immutable_and_reconciled(
    tmp_path: Path,
) -> None:
    path = tmp_path / "network_registry.json"
    payload = {
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
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = NetworkAcquisitionRegistry.load(path, allowed_root=tmp_path)

    assert registry.registry_id == sha256_bytes(canonical_json_bytes(payload))
    with pytest.raises(TypeError):
        registry.allowed_origin_paths["fixture"] = "https://attacker.invalid/data"
    with pytest.raises(TypeError):
        registry.accepted_http_statuses["fixture"] = (200, 201)

    capability = registry._issue_response_capability(
        source="fixture",
        requested_url="https://example.invalid/data?window=bounded",
        response_url="https://example.invalid/data?window=bounded",
        http_status=200,
        raw=b"compatible",
        headers={"content-type": "application/octet-stream"},
    )
    capability.validate(registry=registry)

    replaced = NetworkAcquisitionRegistry.load(path, allowed_root=tmp_path)
    object.__setattr__(
        replaced,
        "allowed_origin_paths",
        {"fixture": "https://attacker.invalid/data"},
    )
    with pytest.raises(ContractError, match="operative policy differs"):
        replaced.validate()


def test_network_registry_must_remain_inside_its_approved_root(
    tmp_path: Path,
) -> None:
    approved_root = tmp_path / "approved"
    approved_root.mkdir()
    outside_path = tmp_path / "outside-network-registry.json"
    payload = {
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
    outside_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="escapes its approved root"):
        NetworkAcquisitionRegistry.load(
            outside_path,
            allowed_root=approved_root,
        )

    inside_path = approved_root / "network-registry.json"
    inside_path.write_text(json.dumps(payload), encoding="utf-8")
    registry = NetworkAcquisitionRegistry.load(
        inside_path,
        allowed_root=approved_root,
    )
    registry.validate()
    assert registry.registry_root == str(approved_root.resolve(strict=True))


def test_zero_byte_network_response_is_bound_then_rejected_as_snapshot_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "network_registry.json"
    registry_path.write_text(
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
    registry = NetworkAcquisitionRegistry.load(
        registry_path,
        allowed_root=tmp_path,
    )
    store = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
        acquisition_registry=registry,
    )
    production_clock = TrustedClock.production()
    plan = NetworkRequestPlan.create(
        registry=registry,
        source="fixture",
        initial_url="https://example.invalid/data",
        timeout_seconds=30,
        max_response_bytes=1024,
        max_pages=1,
        pagination_parameter=None,
    )
    authority, _, _ = _external_authority(tmp_path, monkeypatch)
    receipt = _network_authorization_receipt(
        plan,
        registry=registry,
        authority=authority,
        clock=production_clock,
    )
    authorization_store = NetworkAuthorizationUseStore(
        tmp_path / "authorization-uses",
        allowed_root=tmp_path,
        registry=registry,
    )
    session = authorization_store.authorize(
        plan=plan,
        receipt=receipt,
        authority=authority,
        clock=production_clock,
    )
    request_attempt = assert_authorized_network_request(
        session,
        source="fixture",
        url=plan.initial_url,
        timeout_seconds=30,
        max_response_bytes=1024,
        page_index=0,
        expected_page_token=None,
        clock=production_clock,
    )

    transport_evidence = _bind_authorized_network_response(
        request_attempt,
        requested_url=plan.initial_url,
        response_url=plan.initial_url,
        http_status=200,
        raw=b"",
        headers={"content-type": "application/octet-stream"},
    )
    assert transport_evidence.raw_sha256 == sha256_bytes(b"")
    with pytest.raises(ContractError, match="snapshot response is empty"):
        store._land_network_response(
            transport_evidence=transport_evidence,
            source="fixture",
            requested_url=plan.initial_url,
            response_url=plan.initial_url,
            http_status=200,
            raw=b"",
            headers={"content-type": "application/octet-stream"},
            clock=production_clock,
            max_bytes=1024,
        )


def test_network_snapshot_requires_independent_attestation_for_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "network_registry.json"
    registry_path.write_text(
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
    registry = NetworkAcquisitionRegistry.load(
        registry_path,
        allowed_root=tmp_path,
    )
    store = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
        acquisition_registry=registry,
    )
    production_clock = TrustedClock.production()
    plan = NetworkRequestPlan.create(
        registry=registry,
        source="fixture",
        initial_url="https://example.invalid/data",
        timeout_seconds=30,
        max_response_bytes=1024,
        max_pages=1,
        pagination_parameter=None,
    )
    authority, authority_registry_path, authority_registry = _external_authority(
        tmp_path,
        monkeypatch,
    )
    authorization_receipt = _network_authorization_receipt(
        plan,
        registry=registry,
        authority=authority,
        clock=production_clock,
    )
    authorization_store = NetworkAuthorizationUseStore(
        tmp_path / "authorization-uses",
        allowed_root=tmp_path,
        registry=registry,
    )
    session = authorization_store.authorize(
        plan=plan,
        receipt=authorization_receipt,
        authority=authority,
        clock=production_clock,
    )
    request_attempt = assert_authorized_network_request(
        session,
        source="fixture",
        url=plan.initial_url,
        timeout_seconds=30,
        max_response_bytes=1024,
        page_index=0,
        expected_page_token=None,
        clock=production_clock,
    )
    with pytest.raises(
        EvaluationAuthorizationError,
        match="differs from its authorized request attempt",
    ):
        _bind_authorized_network_response(
            request_attempt,
            requested_url=plan.initial_url,
            response_url="https://example.invalid/redirect",
            http_status=200,
            raw=b"network fixture",
            headers={"content-type": "application/octet-stream"},
        )
    transport_evidence = _bind_authorized_network_response(
        request_attempt,
        requested_url=plan.initial_url,
        response_url=plan.initial_url,
        http_status=200,
        raw=b"network fixture",
        headers={"content-type": "application/octet-stream"},
    )
    with pytest.raises(
        EvaluationAuthorizationError,
        match="lacks transport-issued response evidence",
    ):
        store._land_network_response(
            transport_evidence=object(),  # type: ignore[arg-type]
            source="fixture",
            requested_url=plan.initial_url,
            response_url=plan.initial_url,
            http_status=200,
            raw=b"network fixture",
            headers={"content-type": "application/octet-stream"},
            clock=production_clock,
            max_bytes=1024,
        )
    with pytest.raises(
        EvaluationAuthorizationError,
        match="forged, replayed, or mismatched",
    ):
        store._land_network_response(
            transport_evidence=transport_evidence,
            source="fixture",
            requested_url=plan.initial_url,
            response_url=plan.initial_url,
            http_status=201,
            raw=b"arbitrary replacement",
            headers={"content-type": "application/octet-stream"},
            clock=production_clock,
            max_bytes=1024,
        )
    original_land = store._land

    def fail_landing(**kwargs):
        raise OSError("simulated landing failure")

    monkeypatch.setattr(store, "_land", fail_landing)
    with pytest.raises(OSError, match="simulated landing failure"):
        store._land_network_response(
            transport_evidence=transport_evidence,
            source="fixture",
            requested_url=plan.initial_url,
            response_url=plan.initial_url,
            http_status=200,
            raw=b"network fixture",
            headers={"content-type": "application/octet-stream"},
            clock=production_clock,
            max_bytes=1024,
        )
    monkeypatch.setattr(store, "_land", original_land)
    snapshot = store._land_network_response(
        transport_evidence=transport_evidence,
        source="fixture",
        requested_url="https://example.invalid/data",
        response_url="https://example.invalid/data",
        http_status=200,
        raw=b"network fixture",
        headers={"content-type": "application/octet-stream"},
        clock=production_clock,
        max_bytes=1024,
    )
    with pytest.raises(
        EvaluationAuthorizationError,
        match="forged, replayed, or mismatched",
    ):
        store._land_network_response(
            transport_evidence=transport_evidence,
            source="fixture",
            requested_url=plan.initial_url,
            response_url=plan.initial_url,
            http_status=200,
            raw=b"network fixture",
            headers={"content-type": "application/octet-stream"},
            clock=production_clock,
            max_bytes=1024,
        )
    assert snapshot.trust_eligible is False
    assert store.load(snapshot.root).trust_eligible is False
    assert snapshot.read_verified_bytes() == b"network fixture"

    signing = {
        "schema_version": 1,
        "scope": NETWORK_ACQUISITION_ATTESTATION_SCOPE,
        "snapshot_id": snapshot.snapshot_id,
        "bindings": network_acquisition_attestation_bindings(snapshot),
        "signed_at": snapshot.retrieved_at.isoformat().replace("+00:00", "Z"),
        "key_id": authority.key_id,
        "authority_registry_id": authority.registry_id,
        "authorization_class": authority.authorization_class,
    }
    signature = _rsa_sign_for_fixture(canonical_json_bytes(signing))
    unsigned = {**signing, "signature": signature}
    attestation = SignedNetworkAcquisitionReceipt(
        **signing,
        signature=signature,
        receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    attestation_path = tmp_path / "network-attestation.json"
    attestation_path.write_bytes(canonical_json_bytes(attestation.as_dict()))
    trusted = store.load_attested(
        snapshot.root,
        attestation_path=attestation_path,
        authority=authority,
        clock=TrustedClock.production(),
    )
    assert trusted.trust_eligible is True
    assert trusted.read_verified_bytes() == b"network fixture"

    outside = tmp_path.parent / f"{tmp_path.name}-outside-attestation.json"
    outside.write_bytes(canonical_json_bytes(attestation.as_dict()))
    try:
        with pytest.raises(ContractError, match="escapes its approved root"):
            store.load_attested(
                snapshot.root,
                attestation_path=outside,
                authority=authority,
                clock=TrustedClock.production(),
            )
    finally:
        outside.unlink()

    without_registry = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
    )
    with pytest.raises(IntegrityError, match="requires its pinned acquisition registry"):
        without_registry.load(snapshot.root)

    receipt = json.loads((snapshot.root / "receipt.json").read_text(encoding="utf-8"))
    receipt["acquisition_capability_id"] = "f" * 64
    unsigned = dict(receipt)
    unsigned.pop("snapshot_id")
    receipt["snapshot_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    forged = snapshot.root.parent / receipt["snapshot_id"]
    forged.mkdir()
    (forged / "raw.bin").write_bytes((snapshot.root / "raw.bin").read_bytes())
    (forged / "headers.json").write_bytes(
        (snapshot.root / "headers.json").read_bytes()
    )
    (forged / "receipt.json").write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(IntegrityError, match="differs from the pinned registry"):
        store.load(forged)
    authority_registry["status"] = "REVOKED"
    authority_registry_path.write_text(
        json.dumps(authority_registry),
        encoding="utf-8",
    )
    assert trusted.trust_eligible is False
    with pytest.raises(IntegrityError, match="signature is invalid"):
        trusted.read_verified_bytes()


def test_guarded_alpaca_path_integrates_real_trust_chain_with_transport_only_mocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "alpaca_iex_qualification"
    registry_path = tmp_path / "network_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "US_stocks_swing_model_v2",
                "status": "ACTIVE",
                "allowed_sources": {
                    source: {
                        "origin_path": (
                            "https://data.alpaca.markets/v2/stocks/bars"
                        ),
                        "accepted_http_statuses": [200],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = NetworkAcquisitionRegistry.load(
        registry_path,
        allowed_root=tmp_path,
    )
    store = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
        acquisition_registry=registry,
    )
    clock = TrustedClock.production()
    requested_at = clock.now()
    request = AlpacaBarsRequest(
        symbols=("ABC",),
        start=requested_at - timedelta(days=3),
        end=requested_at - timedelta(days=1),
        requested_at=requested_at,
    )
    policy = AlpacaBarsPolicy(feed="iex")
    plan = NetworkRequestPlan.create(
        registry=registry,
        source=source,
        initial_url=request.url(policy),
        timeout_seconds=30,
        max_response_bytes=MAX_ALPACA_RESPONSE_BYTES,
        max_pages=1,
        pagination_parameter=None,
    )
    authority, _, _ = _external_authority(tmp_path, monkeypatch)
    receipt = _network_authorization_receipt(
        plan,
        registry=registry,
        authority=authority,
        clock=clock,
    )
    authorization_store = NetworkAuthorizationUseStore(
        tmp_path / "authorization-uses",
        allowed_root=tmp_path,
        registry=registry,
    )
    session = authorization_store.authorize(
        plan=plan,
        receipt=receipt,
        authority=authority,
        clock=clock,
    )
    raw = canonical_json_bytes(
        {
            "bars": {
                "ABC": [
                    {
                        "t": "2026-07-20T04:00:00Z",
                        "o": 10,
                        "h": 11,
                        "l": 9,
                        "c": 10.5,
                        "v": 100,
                    }
                ]
            },
            "next_page_token": None,
        }
    )

    class Response:
        status = 200
        headers = {
            "content-length": str(len(raw)),
            "content-type": "application/json",
        }

        def __init__(self, url: str):
            self.url = url

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == MAX_ALPACA_RESPONSE_BYTES + 1
            return raw

        def geturl(self) -> str:
            return self.url

    transport_calls: list[str] = []

    def open_response(
        http_request: object,
        *,
        timeout_seconds: int,
    ) -> Response:
        assert timeout_seconds == 30
        url = str(getattr(http_request, "full_url"))
        transport_calls.append(url)
        return Response(url)

    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    monkeypatch.setattr(alpaca_module, "open_without_redirects", open_response)
    landed = guarded_fetch_landed_pages(
        request,
        snapshot_store=store,
        api_key_id="offline-test-id",
        api_secret_key="offline-test-secret",
        policy=policy,
        network_enabled=True,
        max_pages=1,
        clock=clock,
        authorization_session=session,
    )
    assert transport_calls == [plan.initial_url]
    assert len(landed) == 1
    snapshot = landed[0]
    assert snapshot.trust_eligible is False
    assert snapshot.read_verified_bytes() == raw

    signing = {
        "schema_version": 1,
        "scope": NETWORK_ACQUISITION_ATTESTATION_SCOPE,
        "snapshot_id": snapshot.snapshot_id,
        "bindings": network_acquisition_attestation_bindings(snapshot),
        "signed_at": snapshot.retrieved_at.isoformat().replace("+00:00", "Z"),
        "key_id": authority.key_id,
        "authority_registry_id": authority.registry_id,
        "authorization_class": authority.authorization_class,
    }
    signature = _rsa_sign_for_fixture(canonical_json_bytes(signing))
    unsigned = {**signing, "signature": signature}
    attestation = SignedNetworkAcquisitionReceipt(
        **signing,
        signature=signature,
        receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    attestation_path = tmp_path / "network-attestation.json"
    attestation_path.write_bytes(canonical_json_bytes(attestation.as_dict()))
    trusted = store.load_attested(
        snapshot.root,
        attestation_path=attestation_path,
        authority=authority,
        clock=TrustedClock.production(),
    )
    assert trusted.trust_eligible is True
    assert trusted.read_verified_bytes() == raw

    snapshot.raw_path.write_bytes(raw + b"\n")
    with pytest.raises(IntegrityError, match="snapshot raw bytes differ from receipt"):
        trusted.read_verified_bytes()


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
    for field in (
        "project",
        "dataset",
        "source_epoch",
        "role",
        "quality_state",
        "created_at",
    ):
        text_poison = manifest.as_dict()
        text_poison[field] = 123
        with pytest.raises(ContractError, match="exact JSON strings"):
            ReleaseManifest.from_dict(text_poison)
    file_poison = manifest.as_dict()
    file_poison["files"][0]["path"] = 123
    with pytest.raises(ContractError, match="exact JSON field types"):
        ReleaseManifest.from_dict(file_poison)
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    with pytest.raises(ContractError, match="escapes its approved root"):
        verify_accepted_release(stage, accepted_root=accepted_root)


def test_sha256_fields_reject_uppercase_shape_only_values() -> None:
    with pytest.raises(ContractError, match="exact lowercase SHA-256"):
        require_sha256("A" * 64, "audit.uppercase_hash")


@pytest.mark.parametrize("value", [None, 1, True, [], object()])
def test_parse_timestamp_rejects_non_string_inputs_as_contract_errors(
    value: object,
) -> None:
    with pytest.raises(ContractError, match="must be exact ISO-8601 text"):
        parse_timestamp(value, "audit.timestamp")  # type: ignore[arg-type]


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


def test_production_registry_rejects_known_fixture_authority_and_private_material_is_test_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_jwk = _external_public_jwk()
    registry_path = tmp_path / "production-authorities.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": "US_stocks_swing_model_v2",
                "status": "ACTIVE",
                "authorities": [
                    {
                        "key_id": "RFC7515-TEST-FIXTURE-ONLY",
                        "key_sha256": sha256_bytes(public_jwk),
                        "authorization_class": "EXTERNAL_USER_AUTHORITY",
                        "signature_algorithm": EXTERNAL_SIGNATURE_ALGORITHM,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        governance_module,
        "PRODUCTION_AUTHORITY_REGISTRY_PATH",
        registry_path,
    )
    with pytest.raises(
        EvaluationAuthorizationError,
        match="prohibited test fixture material",
    ):
        load_external_authority(
            registry_path,
            key_id="RFC7515-TEST-FIXTURE-ONLY",
            verification_key=public_jwk,
        )

    source_root = Path(__file__).parents[1] / "src"
    for source in source_root.rglob("*.py"):
        tree = ast.parse(
            source.read_text(encoding="utf-8"),
            filename=str(source),
        )
        assert all(
            not (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == _RFC7515_RSA_D
            )
            for node in ast.walk(tree)
        ), f"fixture private exponent escaped tests: {source}"
