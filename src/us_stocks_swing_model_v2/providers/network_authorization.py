from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
from pathlib import Path
import re
import secrets
from typing import Mapping
from urllib.parse import parse_qsl, urlparse
from weakref import WeakKeyDictionary

from ..clock import TrustedClock, require_trusted_clock
from ..common import (
    atomic_write,
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_contained_path,
    sha256_bytes,
)
from ..errors import ContractError, EvaluationAuthorizationError
from ..governance import AuthorizationAuthority, SignedAuthorizationReceipt
from ..locking import ExclusiveFileLock
from .snapshots import NetworkAcquisitionRegistry


NETWORK_ACQUISITION_AUTHORIZATION_SCOPE = "AUTHORIZE_NETWORK_ACQUISITION"
MAX_NETWORK_AUTHORIZATION_LIFETIME = timedelta(minutes=10)
_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class NetworkRequestPlan:
    source: str
    initial_url: str
    method: str
    timeout_seconds: int
    max_response_bytes: int
    max_pages: int
    pagination_parameter: str
    network_registry_id: str
    plan_id: str

    @classmethod
    def create(
        cls,
        *,
        registry: NetworkAcquisitionRegistry,
        source: str,
        initial_url: str,
        timeout_seconds: int,
        max_response_bytes: int,
        max_pages: int,
        pagination_parameter: str | None,
    ) -> "NetworkRequestPlan":
        registry.validate()
        parsed = urlparse(initial_url)
        origin_path = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if registry.allowed_origin_paths.get(source) != origin_path:
            raise ContractError("network authorization plan is outside the pinned registry")
        if parsed.scheme != "https" or parsed.fragment or parsed.username or parsed.password:
            raise ContractError("network authorization URL is invalid")
        if not 1 <= timeout_seconds <= 30:
            raise ContractError("network authorization timeout must be in [1,30]")
        if not 1 <= max_response_bytes <= 64 * 1024 * 1024:
            raise ContractError("network authorization response limit is invalid")
        if not 1 <= max_pages <= 100:
            raise ContractError("network authorization page limit must be in [1,100]")
        pagination = pagination_parameter or "none"
        if pagination not in {"none", "page_token"}:
            raise ContractError("network authorization pagination parameter is invalid")
        if pagination == "none" and max_pages != 1:
            raise ContractError("non-paginated authorization must allow exactly one page")
        unsigned = {
            "source": source,
            "initial_url": initial_url,
            "method": "GET",
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
            "max_pages": max_pages,
            "pagination_parameter": pagination,
            "network_registry_id": registry.registry_id,
        }
        return cls(**unsigned, plan_id=sha256_bytes(canonical_json_bytes(unsigned)))

    def bindings(self, *, nonce: str) -> dict[str, str]:
        if not _NONCE.fullmatch(nonce):
            raise EvaluationAuthorizationError(
                "network authorization nonce must be 256-bit base64url"
            )
        return {
            "authorization_nonce": nonce,
            "initial_url": self.initial_url,
            "max_pages": str(self.max_pages),
            "max_response_bytes": str(self.max_response_bytes),
            "method": self.method,
            "network_registry_id": self.network_registry_id,
            "pagination_parameter": self.pagination_parameter,
            "project": "US_stocks_swing_model_v2",
            "source": self.source,
            "timeout_seconds": str(self.timeout_seconds),
        }


@dataclass(frozen=True, init=False, eq=False)
class NetworkAuthorizationSession:
    plan: NetworkRequestPlan
    receipt_id: str
    nonce: str
    consumed_at: str

    def _assert_request(
        self,
        *,
        source: str,
        url: str,
        timeout_seconds: int,
        max_response_bytes: int,
        page_index: int,
        expected_page_token: str | None,
    ) -> None:
        if (
            source != self.plan.source
            or timeout_seconds != self.plan.timeout_seconds
            or max_response_bytes != self.plan.max_response_bytes
            or not 0 <= page_index < self.plan.max_pages
        ):
            raise EvaluationAuthorizationError(
                "network request exceeds its signed authorization"
            )
        if page_index == 0:
            if url != self.plan.initial_url or expected_page_token is not None:
                raise EvaluationAuthorizationError(
                    "initial network request differs from its authorization"
                )
            return
        if self.plan.pagination_parameter != "page_token" or not expected_page_token:
            raise EvaluationAuthorizationError(
                "network pagination is not authorized"
            )
        initial = urlparse(self.plan.initial_url)
        current = urlparse(url)
        if (current.scheme, current.netloc, current.path) != (
            initial.scheme,
            initial.netloc,
            initial.path,
        ):
            raise EvaluationAuthorizationError("paginated request changed origin or path")
        initial_query = dict(parse_qsl(initial.query, keep_blank_values=True))
        current_query = dict(parse_qsl(current.query, keep_blank_values=True))
        if initial_query.get("page_token") is not None:
            raise EvaluationAuthorizationError(
                "initial authorized URL cannot contain a page token"
            )
        if current_query.pop("page_token", None) != expected_page_token:
            raise EvaluationAuthorizationError(
                "paginated request token differs from verified response"
            )
        if current_query != initial_query:
            raise EvaluationAuthorizationError(
                "paginated request changed signed query parameters"
            )


_ISSUED_SESSIONS: WeakKeyDictionary[
    NetworkAuthorizationSession, tuple[Path, str]
] = WeakKeyDictionary()


def assert_authorized_network_request(
    session: NetworkAuthorizationSession | None,
    *,
    source: str,
    url: str,
    timeout_seconds: int,
    max_response_bytes: int,
    page_index: int,
    expected_page_token: str | None,
) -> None:
    if type(session) is not NetworkAuthorizationSession:
        raise EvaluationAuthorizationError(
            "network request lacks a store-issued authorization session"
        )
    issued = _ISSUED_SESSIONS.get(session)
    if issued is None:
        raise EvaluationAuthorizationError(
            "network authorization session was not issued by the use store"
        )
    marker_path, marker_sha256 = issued
    try:
        marker_bytes = marker_path.read_bytes()
        marker = json.loads(marker_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationAuthorizationError(
            "network authorization consumption marker is unavailable"
        ) from exc
    if (
        sha256_bytes(marker_bytes) != marker_sha256
        or not isinstance(marker, dict)
        or marker.get("plan_id") != session.plan.plan_id
        or marker.get("receipt_id") != session.receipt_id
        or marker.get("authorization_nonce") != session.nonce
        or marker.get("consumed_at") != session.consumed_at
    ):
        raise EvaluationAuthorizationError(
            "network authorization consumption marker differs"
        )
    session._assert_request(
        source=source,
        url=url,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        page_index=page_index,
        expected_page_token=expected_page_token,
    )


class NetworkAuthorizationUseStore:
    def __init__(self, root: Path, *, allowed_root: Path):
        self.root = Path(root)
        self.allowed_root = Path(allowed_root)
        if not self.root.is_absolute() or not self.allowed_root.is_absolute():
            raise ContractError("network authorization use paths must be absolute")
        require_contained_path(self.root, self.allowed_root, must_exist=False)

    def authorize(
        self,
        *,
        plan: NetworkRequestPlan,
        receipt: SignedAuthorizationReceipt,
        authority: AuthorizationAuthority,
        clock: TrustedClock,
    ) -> NetworkAuthorizationSession:
        trusted_clock = require_trusted_clock(clock)
        if authority.authorization_class != "EXTERNAL_USER_AUTHORITY":
            raise EvaluationAuthorizationError(
                "network execution requires an external signing authority"
            )
        nonce = str(receipt.bindings.get("authorization_nonce", ""))
        required = plan.bindings(nonce=nonce)
        issued = parse_utc_z(receipt.issued_at, "network_authorization.issued_at")
        expires = parse_utc_z(receipt.expires_at, "network_authorization.expires_at")
        if expires - issued > MAX_NETWORK_AUTHORIZATION_LIFETIME:
            raise EvaluationAuthorizationError(
                "network authorization lifetime exceeds ten minutes"
            )
        observed = trusted_clock.now()
        receipt.validate_at(
            authority=authority,
            expected_scope=NETWORK_ACQUISITION_AUTHORIZATION_SCOPE,
            expected_subject_id=plan.plan_id,
            required_bindings=required,
            observed_at=observed,
        )
        uses = self.root / "uses"
        marker = uses / f"{receipt.receipt_id}.json"
        lock_path = self.root / ".locks" / f"{receipt.receipt_id}.lock"
        with ExclusiveFileLock(lock_path, allowed_root=self.allowed_root):
            require_contained_path(marker, self.allowed_root, must_exist=False)
            if marker.exists():
                raise EvaluationAuthorizationError(
                    "network authorization receipt has already been consumed"
                )
            marker_payload = canonical_json_bytes(
                {
                    "schema_version": 1,
                    "scope": NETWORK_ACQUISITION_AUTHORIZATION_SCOPE,
                    "plan_id": plan.plan_id,
                    "receipt_id": receipt.receipt_id,
                    "authorization_nonce": nonce,
                    "consumed_at": iso_z(observed),
                    "time_authority": trusted_clock.mode,
                }
            )
            atomic_write(
                marker,
                marker_payload,
            )
        session = object.__new__(NetworkAuthorizationSession)
        object.__setattr__(session, "plan", plan)
        object.__setattr__(session, "receipt_id", receipt.receipt_id)
        object.__setattr__(session, "nonce", nonce)
        object.__setattr__(session, "consumed_at", iso_z(observed))
        _ISSUED_SESSIONS[session] = (marker, sha256_bytes(marker_payload))
        return session


def network_authorization_request(
    plan: NetworkRequestPlan,
    *,
    clock: TrustedClock,
    nonce: str | None = None,
) -> Mapping[str, object]:
    issued = require_trusted_clock(clock).now()
    chosen_nonce = nonce or secrets.token_urlsafe(32)
    bindings = plan.bindings(nonce=chosen_nonce)
    return {
        "schema_version": 1,
        "scope": NETWORK_ACQUISITION_AUTHORIZATION_SCOPE,
        "subject_id": plan.plan_id,
        "bindings": bindings,
        "issued_at": iso_z(issued),
        "expires_at": iso_z(issued + MAX_NETWORK_AUTHORIZATION_LIFETIME),
    }


def assemble_network_authorization_receipt(
    request: Mapping[str, object],
    *,
    signature_hex: str,
    authority: AuthorizationAuthority,
    clock: TrustedClock,
) -> SignedAuthorizationReceipt:
    signing_payload = network_authorization_signing_payload(
        request,
        authority=authority,
    )
    signing = json.loads(signing_payload)
    unsigned = {**signing, "signature": signature_hex}
    receipt = SignedAuthorizationReceipt.from_dict(
        {
            **unsigned,
            "receipt_id": sha256_bytes(canonical_json_bytes(unsigned)),
        }
    )
    receipt.validate_at(
        authority=authority,
        expected_scope=NETWORK_ACQUISITION_AUTHORIZATION_SCOPE,
        expected_subject_id=receipt.subject_id,
        required_bindings=receipt.bindings,
        observed_at=require_trusted_clock(clock).now(),
    )
    return receipt


def network_authorization_signing_payload(
    request: Mapping[str, object],
    *,
    authority: AuthorizationAuthority,
) -> bytes:
    if set(request) != {
        "schema_version",
        "scope",
        "subject_id",
        "bindings",
        "issued_at",
        "expires_at",
    } or not isinstance(request.get("bindings"), dict):
        raise EvaluationAuthorizationError(
            "network authorization request fields differ"
        )
    if authority.authorization_class != "EXTERNAL_USER_AUTHORITY":
        raise EvaluationAuthorizationError(
            "network authorization signing payload requires external authority"
        )
    nonce = str(request["bindings"].get("authorization_nonce", ""))
    if not _NONCE.fullmatch(nonce):
        raise EvaluationAuthorizationError(
            "network authorization nonce must be 256-bit base64url"
        )
    issued = parse_utc_z(str(request["issued_at"]), "network_authorization.issued_at")
    expires = parse_utc_z(
        str(request["expires_at"]), "network_authorization.expires_at"
    )
    if issued >= expires or expires - issued > MAX_NETWORK_AUTHORIZATION_LIFETIME:
        raise EvaluationAuthorizationError(
            "network authorization lifetime exceeds ten minutes"
        )
    signing = {
        **request,
        "key_id": authority.key_id,
        "authority_registry_id": authority.registry_id,
        "authorization_class": authority.authorization_class,
    }
    return canonical_json_bytes(signing)
