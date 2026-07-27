from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Callable, Mapping, TypeVar
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
from .snapshots import NetworkAcquisitionRegistry, normalize_response_headers


NETWORK_ACQUISITION_AUTHORIZATION_SCOPE = "AUTHORIZE_NETWORK_ACQUISITION"
MAX_NETWORK_AUTHORIZATION_LIFETIME = timedelta(minutes=10)
MAX_AUTHORIZATION_CLOCK_DRIFT = timedelta(seconds=5)
AUTHORIZATION_TIME_FLOOR_SCHEMA_VERSION = 1
_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _exact_query_parts(query: str) -> tuple[tuple[str, str, str], ...]:
    raw_parts = query.split("&") if query else []
    try:
        parsed = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise ContractError("network authorization query is malformed") from exc
    if len(parsed) != len(raw_parts):
        raise ContractError("network authorization query is not exact")
    keys = [key for key, _ in parsed]
    if len(keys) != len(set(keys)):
        raise ContractError("network authorization query keys must be unique")
    return tuple(
        (raw, key, value)
        for raw, (key, value) in zip(raw_parts, parsed, strict=True)
    )


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

    def _unsigned(self) -> dict[str, object]:
        return {
            "source": self.source,
            "initial_url": self.initial_url,
            "method": self.method,
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "max_pages": self.max_pages,
            "pagination_parameter": self.pagination_parameter,
            "network_registry_id": self.network_registry_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._unsigned(), "plan_id": self.plan_id}

    def validate(self, *, registry: NetworkAcquisitionRegistry) -> None:
        if type(registry) is not NetworkAcquisitionRegistry:
            raise ContractError("network authorization plan requires its pinned registry")
        registry.validate()
        if (
            type(self.source) is not str
            or type(self.initial_url) is not str
            or self.method != "GET"
            or type(self.timeout_seconds) is not int
            or type(self.max_response_bytes) is not int
            or type(self.max_pages) is not int
            or type(self.pagination_parameter) is not str
            or type(self.network_registry_id) is not str
            or type(self.plan_id) is not str
        ):
            raise ContractError("network authorization plan fields have invalid types")
        parsed = urlparse(self.initial_url)
        origin_path = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if registry.allowed_origin_paths.get(self.source) != origin_path:
            raise ContractError("network authorization plan is outside the pinned registry")
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ContractError("network authorization URL is invalid")
        query_parts = _exact_query_parts(parsed.query)
        if any(key == "page_token" for _, key, _ in query_parts):
            raise ContractError(
                "initial authorized URL cannot contain a page token"
            )
        if not 1 <= self.timeout_seconds <= 30:
            raise ContractError("network authorization timeout must be in [1,30]")
        if not 1 <= self.max_response_bytes <= 64 * 1024 * 1024:
            raise ContractError("network authorization response limit is invalid")
        if not 1 <= self.max_pages <= 100:
            raise ContractError("network authorization page limit must be in [1,100]")
        if self.pagination_parameter not in {"none", "page_token"}:
            raise ContractError("network authorization pagination parameter is invalid")
        if self.pagination_parameter == "none" and self.max_pages != 1:
            raise ContractError("non-paginated authorization must allow exactly one page")
        if self.network_registry_id != registry.registry_id:
            raise ContractError("network authorization plan registry binding differs")
        expected_plan_id = sha256_bytes(canonical_json_bytes(self._unsigned()))
        if self.plan_id != expected_plan_id:
            raise ContractError("network authorization plan ID differs from its exact fields")

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
        if type(registry) is not NetworkAcquisitionRegistry:
            raise ContractError("network authorization plan requires its pinned registry")
        registry.validate()
        if pagination_parameter is None:
            pagination = "none"
        elif type(pagination_parameter) is str:
            pagination = pagination_parameter
        else:
            raise ContractError(
                "network authorization pagination parameter is invalid"
            )
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
        plan = cls(**unsigned, plan_id=sha256_bytes(canonical_json_bytes(unsigned)))
        plan.validate(registry=registry)
        return plan

    def bindings(
        self,
        *,
        nonce: str,
        registry: NetworkAcquisitionRegistry,
    ) -> dict[str, str]:
        self.validate(registry=registry)
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
    expires_at: str
    time_floor_id: str

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
        try:
            initial_query = _exact_query_parts(initial.query)
            current_query = _exact_query_parts(current.query)
        except ContractError as exc:
            raise EvaluationAuthorizationError(str(exc)) from exc
        page_tokens = [
            value
            for _, key, value in current_query
            if key == "page_token"
        ]
        if page_tokens != [expected_page_token]:
            raise EvaluationAuthorizationError(
                "paginated request token differs from verified response"
            )
        current_base_query = "&".join(
            raw
            for raw, key, _ in current_query
            if key != "page_token"
        )
        initial_base_query = "&".join(raw for raw, _, _ in initial_query)
        if current_base_query != initial_base_query:
            raise EvaluationAuthorizationError(
                "paginated request changed signed query parameters"
            )


@dataclass(frozen=True, init=False, eq=False)
class LocalNetworkExecutionSession:
    """Process-local owner session for one exact bounded request plan."""

    plan: NetworkRequestPlan
    session_id: str
    started_at: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "local network sessions must be issued from a validated request plan"
        )

    def _assert_request(self, **kwargs: object) -> None:
        NetworkAuthorizationSession._assert_request(self, **kwargs)


_ISSUED_SESSIONS: WeakKeyDictionary[
    NetworkAuthorizationSession, dict[str, object]
] = WeakKeyDictionary()
_ISSUED_LOCAL_SESSIONS: WeakKeyDictionary[
    LocalNetworkExecutionSession, dict[str, object]
] = WeakKeyDictionary()
_ISSUED_SESSIONS_LOCK = threading.Lock()
_COMMIT_RESULT = TypeVar("_COMMIT_RESULT")


@dataclass(frozen=True, init=False, eq=False)
class AuthorizedNetworkRequestAttempt:
    attempt_id: str
    plan_id: str
    receipt_id: str
    source: str
    requested_url: str
    timeout_seconds: int
    max_response_bytes: int
    page_index: int
    expires_at: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "network request attempts must be issued by authorization preflight"
        )


@dataclass(frozen=True, init=False, eq=False)
class AuthorizedNetworkResponse:
    response_id: str
    attempt_id: str
    source: str
    requested_url: str
    response_url: str
    http_status: int
    raw_sha256: str
    headers_sha256: str
    max_response_bytes: int

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "network response evidence must be issued by the guarded transport"
        )


_ISSUED_REQUEST_ATTEMPTS: WeakKeyDictionary[
    AuthorizedNetworkRequestAttempt, dict[str, object]
] = WeakKeyDictionary()
_ISSUED_NETWORK_RESPONSES: WeakKeyDictionary[
    AuthorizedNetworkResponse, dict[str, object]
] = WeakKeyDictionary()


def _advance_authorization_time_floor(
    *,
    root: Path,
    allowed_root: Path,
    network_registry_id: str,
    clock: TrustedClock,
) -> tuple[datetime, str]:
    """Persist a rollback-resistant wall/monotonic authorization time floor."""

    trusted_clock = require_trusted_clock(clock)
    floor_path = require_contained_path(
        root / "authorization-time-floor.json",
        allowed_root,
        must_exist=False,
    )
    lock_path = require_contained_path(
        root / ".locks" / "authorization-time-floor.lock",
        allowed_root,
        must_exist=False,
    )
    with ExclusiveFileLock(lock_path, allowed_root=allowed_root):
        observed = trusted_clock.now()
        monotonic_ns = time.monotonic_ns()
        if floor_path.exists():
            try:
                payload_bytes = floor_path.read_bytes()
                payload = json.loads(payload_bytes)
            except (OSError, json.JSONDecodeError) as exc:
                raise EvaluationAuthorizationError(
                    "network authorization time floor is unavailable or invalid"
                ) from exc
            expected_fields = {
                "schema_version",
                "network_registry_id",
                "time_authority",
                "observed_at",
                "monotonic_ns",
                "time_floor_id",
            }
            if (
                not isinstance(payload, dict)
                or set(payload) != expected_fields
                or type(payload.get("schema_version")) is not int
                or payload.get("schema_version")
                != AUTHORIZATION_TIME_FLOOR_SCHEMA_VERSION
                or payload.get("network_registry_id") != network_registry_id
                or payload.get("time_authority") != trusted_clock.mode
                or type(payload.get("monotonic_ns")) is not int
                or payload["monotonic_ns"] < 0
            ):
                raise EvaluationAuthorizationError(
                    "network authorization time floor differs from its contract"
                )
            unsigned_prior = {
                key: value
                for key, value in payload.items()
                if key != "time_floor_id"
            }
            if payload.get("time_floor_id") != sha256_bytes(
                canonical_json_bytes(unsigned_prior)
            ):
                raise EvaluationAuthorizationError(
                    "network authorization time floor integrity differs"
                )
            prior_observed = parse_utc_z(
                payload["observed_at"],
                "network_authorization_time_floor.observed_at",
            )
            prior_monotonic_ns = payload["monotonic_ns"]
            if monotonic_ns < prior_monotonic_ns:
                raise EvaluationAuthorizationError(
                    "network authorization monotonic epoch changed; "
                    "time-floor recovery is required"
                )
            elapsed = timedelta(
                microseconds=(monotonic_ns - prior_monotonic_ns) // 1_000
            )
            expected = prior_observed + elapsed
            if (
                observed < expected - MAX_AUTHORIZATION_CLOCK_DRIFT
                or observed > expected + MAX_AUTHORIZATION_CLOCK_DRIFT
            ):
                raise EvaluationAuthorizationError(
                    "network authorization clock moved outside the allowed drift"
                )
        unsigned = {
            "schema_version": AUTHORIZATION_TIME_FLOOR_SCHEMA_VERSION,
            "network_registry_id": network_registry_id,
            "time_authority": trusted_clock.mode,
            "observed_at": iso_z(observed),
            "monotonic_ns": monotonic_ns,
        }
        time_floor_id = sha256_bytes(canonical_json_bytes(unsigned))
        atomic_write(
            floor_path,
            canonical_json_bytes(
                {
                    **unsigned,
                    "time_floor_id": time_floor_id,
                }
            ),
        )
        return observed, time_floor_id


def assert_authorized_network_request(
    session: NetworkAuthorizationSession | None,
    *,
    source: str,
    url: str,
    timeout_seconds: int,
    max_response_bytes: int,
    page_index: int,
    expected_page_token: str | None,
    clock: TrustedClock,
) -> AuthorizedNetworkRequestAttempt:
    """Consume one authorized request attempt before any transport I/O.

    Consumption is deliberately irreversible: a timeout, interruption, or
    malformed response spends this page index. Retrying requires a fresh
    externally signed authorization receipt and store-issued session.
    """

    if type(session) is not NetworkAuthorizationSession:
        raise EvaluationAuthorizationError(
            "network request lacks a store-issued authorization session"
        )
    with _ISSUED_SESSIONS_LOCK:
        issued = _ISSUED_SESSIONS.get(session)
        if issued is None:
            raise EvaluationAuthorizationError(
                "network authorization session was not issued by the use store"
            )
        marker_path = issued["marker_path"]
        assert isinstance(marker_path, Path)
        try:
            marker_bytes = marker_path.read_bytes()
            marker = json.loads(marker_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise EvaluationAuthorizationError(
                "network authorization consumption marker is unavailable"
            ) from exc
        if (
            sha256_bytes(marker_bytes) != issued["marker_sha256"]
            or not isinstance(marker, dict)
            or marker.get("plan_id") != session.plan.plan_id
            or marker.get("receipt_id") != session.receipt_id
            or marker.get("authorization_nonce") != session.nonce
            or marker.get("consumed_at") != session.consumed_at
            or marker.get("expires_at") != session.expires_at
            or marker.get("time_floor_id") != session.time_floor_id
        ):
            raise EvaluationAuthorizationError(
                "network authorization consumption marker differs"
            )
        trusted_clock = require_trusted_clock(clock)
        expires_at = parse_utc_z(
            session.expires_at, "network_authorization.expires_at"
        )
        if trusted_clock.now() >= expires_at:
            raise EvaluationAuthorizationError(
                "network authorization session has expired"
            )
        observed, _ = _advance_authorization_time_floor(
            root=issued["time_floor_root"],
            allowed_root=issued["allowed_root"],
            network_registry_id=issued["network_registry_id"],
            clock=trusted_clock,
        )
        if observed >= expires_at:
            raise EvaluationAuthorizationError(
                "network authorization session has expired"
            )
        if page_index != issued["next_page"]:
            raise EvaluationAuthorizationError(
                "network authorization page was reused or is out of sequence"
            )
        registry = issued["registry"]
        if type(registry) is not NetworkAcquisitionRegistry:
            raise EvaluationAuthorizationError(
                "network authorization session lacks its pinned registry"
            )
        try:
            session.plan.validate(registry=registry)
        except ContractError as exc:
            raise EvaluationAuthorizationError(
                "network authorization session plan is invalid"
            ) from exc
        session._assert_request(
            source=source,
            url=url,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            page_index=page_index,
            expected_page_token=expected_page_token,
        )
        issued["next_page"] = page_index + 1
        attempt_unsigned = {
            "plan_id": session.plan.plan_id,
            "receipt_id": session.receipt_id,
            "source": source,
            "requested_url": url,
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
            "page_index": page_index,
            "expires_at": session.expires_at,
            "attempt_nonce": secrets.token_urlsafe(32),
        }
        attempt = object.__new__(AuthorizedNetworkRequestAttempt)
        attempt_fields = {
            key: value
            for key, value in attempt_unsigned.items()
            if key != "attempt_nonce"
        }
        attempt_fields["attempt_id"] = sha256_bytes(
            canonical_json_bytes(attempt_unsigned)
        )
        for name, value in attempt_fields.items():
            object.__setattr__(attempt, name, value)
        _ISSUED_REQUEST_ATTEMPTS[attempt] = {
            "attempt_id": attempt.attempt_id,
            "bound": False,
        }
        return attempt


def start_local_network_execution(
    plan: NetworkRequestPlan,
    *,
    registry: NetworkAcquisitionRegistry,
    clock: TrustedClock,
) -> LocalNetworkExecutionSession:
    """Issue an in-memory session after explicit local-owner confirmation."""

    if type(plan) is not NetworkRequestPlan:
        raise EvaluationAuthorizationError(
            "local network execution requires an exact request plan"
        )
    plan.validate(registry=registry)
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise EvaluationAuthorizationError(
            "local network execution requires the production UTC clock"
        )
    started_at = iso_z(trusted_clock.now())
    unsigned = {
        "schema_version": 1,
        "mode": "OWNER_OPERATED_LOCAL",
        "project": "US_stocks_swing_model_v2",
        "plan_id": plan.plan_id,
        "network_registry_id": registry.registry_id,
        "started_at": started_at,
        "session_nonce": secrets.token_urlsafe(32),
    }
    session = object.__new__(LocalNetworkExecutionSession)
    object.__setattr__(session, "plan", plan)
    object.__setattr__(
        session,
        "session_id",
        sha256_bytes(canonical_json_bytes(unsigned)),
    )
    object.__setattr__(session, "started_at", started_at)
    with _ISSUED_SESSIONS_LOCK:
        _ISSUED_LOCAL_SESSIONS[session] = {
            "session_id": session.session_id,
            "registry": registry,
            "next_page": 0,
        }
    return session


def assert_local_network_request(
    session: LocalNetworkExecutionSession | None,
    *,
    source: str,
    url: str,
    timeout_seconds: int,
    max_response_bytes: int,
    page_index: int,
    expected_page_token: str | None,
    clock: TrustedClock,
) -> AuthorizedNetworkRequestAttempt:
    """Spend one ordered request attempt from a local-owner session."""

    if type(session) is not LocalNetworkExecutionSession:
        raise EvaluationAuthorizationError(
            "network request lacks a local owner execution session"
        )
    with _ISSUED_SESSIONS_LOCK:
        issued = _ISSUED_LOCAL_SESSIONS.get(session)
        if issued is None or issued.get("session_id") != session.session_id:
            raise EvaluationAuthorizationError(
                "local network execution session was forged or is unavailable"
            )
        trusted_clock = require_trusted_clock(clock)
        if not trusted_clock.trust_eligible:
            raise EvaluationAuthorizationError(
                "local network execution requires the production UTC clock"
            )
        registry = issued.get("registry")
        if type(registry) is not NetworkAcquisitionRegistry:
            raise EvaluationAuthorizationError(
                "local network session lacks its pinned registry"
            )
        try:
            session.plan.validate(registry=registry)
        except ContractError as exc:
            raise EvaluationAuthorizationError(
                "local network request plan is invalid"
            ) from exc
        if page_index != issued.get("next_page"):
            raise EvaluationAuthorizationError(
                "local network page was reused or is out of sequence"
            )
        session._assert_request(
            source=source,
            url=url,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            page_index=page_index,
            expected_page_token=expected_page_token,
        )
        issued["next_page"] = page_index + 1
        attempt_unsigned = {
            "plan_id": session.plan.plan_id,
            "receipt_id": session.session_id,
            "source": source,
            "requested_url": url,
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
            "page_index": page_index,
            "expires_at": session.started_at,
            "attempt_nonce": secrets.token_urlsafe(32),
        }
        attempt = object.__new__(AuthorizedNetworkRequestAttempt)
        attempt_fields = {
            key: value
            for key, value in attempt_unsigned.items()
            if key != "attempt_nonce"
        }
        attempt_fields["attempt_id"] = sha256_bytes(
            canonical_json_bytes(attempt_unsigned)
        )
        for name, value in attempt_fields.items():
            object.__setattr__(attempt, name, value)
        _ISSUED_REQUEST_ATTEMPTS[attempt] = {
            "attempt_id": attempt.attempt_id,
            "bound": False,
        }
        return attempt


def _bind_authorized_network_response(
    attempt: AuthorizedNetworkRequestAttempt,
    *,
    requested_url: str,
    response_url: str,
    http_status: int,
    raw: bytes,
    headers: Mapping[str, str],
) -> AuthorizedNetworkResponse:
    """Bind one guarded transport result to one spent request attempt."""

    if type(attempt) is not AuthorizedNetworkRequestAttempt:
        raise EvaluationAuthorizationError(
            "network response lacks an authorized request attempt"
        )
    with _ISSUED_SESSIONS_LOCK:
        issued = _ISSUED_REQUEST_ATTEMPTS.get(attempt)
        if (
            issued is None
            or issued.get("attempt_id") != attempt.attempt_id
            or issued.get("bound") is not False
        ):
            raise EvaluationAuthorizationError(
                "network request attempt was forged or already bound"
            )
        if (
            requested_url != attempt.requested_url
            or response_url != requested_url
            or type(http_status) is not int
            or not 100 <= http_status <= 599
            or type(raw) is not bytes
            or len(raw) > attempt.max_response_bytes
        ):
            raise EvaluationAuthorizationError(
                "network response differs from its authorized request attempt"
            )
        normalized_headers = normalize_response_headers(headers)
        unsigned = {
            "attempt_id": attempt.attempt_id,
            "source": attempt.source,
            "requested_url": requested_url,
            "response_url": response_url,
            "http_status": http_status,
            "raw_sha256": sha256_bytes(raw),
            "headers_sha256": sha256_bytes(
                canonical_json_bytes(normalized_headers)
            ),
            "max_response_bytes": attempt.max_response_bytes,
        }
        response = object.__new__(AuthorizedNetworkResponse)
        response_fields = {
            **unsigned,
            "response_id": sha256_bytes(canonical_json_bytes(unsigned)),
        }
        for name, value in response_fields.items():
            object.__setattr__(response, name, value)
        issued["bound"] = True
        _ISSUED_NETWORK_RESPONSES[response] = {
            "response_id": response.response_id,
            "consumed": False,
        }
        return response


def _validate_authorized_network_response_unlocked(
    response: AuthorizedNetworkResponse,
    *,
    source: str,
    requested_url: str,
    response_url: str,
    http_status: int,
    raw: bytes,
    headers: Mapping[str, str],
    max_response_bytes: int,
) -> dict[str, object]:
    if type(response) is not AuthorizedNetworkResponse:
        raise EvaluationAuthorizationError(
            "network landing lacks transport-issued response evidence"
        )
    issued = _ISSUED_NETWORK_RESPONSES.get(response)
    normalized_headers = normalize_response_headers(headers)
    if (
        issued is None
        or issued.get("response_id") != response.response_id
        or issued.get("consumed") is not False
        or response.source != source
        or response.requested_url != requested_url
        or response.response_url != response_url
        or response.http_status != http_status
        or response.raw_sha256 != sha256_bytes(raw)
        or response.headers_sha256
        != sha256_bytes(canonical_json_bytes(normalized_headers))
        or response.max_response_bytes != max_response_bytes
    ):
        raise EvaluationAuthorizationError(
            "network response evidence was forged, replayed, or mismatched"
        )
    return issued


def _validate_authorized_network_response(
    response: AuthorizedNetworkResponse,
    **expected: object,
) -> None:
    """Check one issued response without consuming it before landing validation."""

    with _ISSUED_SESSIONS_LOCK:
        _validate_authorized_network_response_unlocked(response, **expected)


def _consume_authorized_network_response(
    response: AuthorizedNetworkResponse,
    *,
    source: str,
    requested_url: str,
    response_url: str,
    http_status: int,
    raw: bytes,
    headers: Mapping[str, str],
    max_response_bytes: int,
    commit: Callable[[], _COMMIT_RESULT],
) -> _COMMIT_RESULT:
    """Validate once, commit landing, then atomically mark the response spent."""

    with _ISSUED_SESSIONS_LOCK:
        issued = _validate_authorized_network_response_unlocked(
            response,
            source=source,
            requested_url=requested_url,
            response_url=response_url,
            http_status=http_status,
            raw=raw,
            headers=headers,
            max_response_bytes=max_response_bytes,
        )
        result = commit()
        issued["consumed"] = True
        return result


class NetworkAuthorizationUseStore:
    def __init__(
        self,
        root: Path,
        *,
        allowed_root: Path,
        registry: NetworkAcquisitionRegistry,
    ):
        self.root = Path(root)
        self.allowed_root = Path(allowed_root)
        self.registry = registry
        if not self.root.is_absolute() or not self.allowed_root.is_absolute():
            raise ContractError("network authorization use paths must be absolute")
        require_contained_path(self.root, self.allowed_root, must_exist=False)
        if type(self.registry) is not NetworkAcquisitionRegistry:
            raise ContractError(
                "network authorization use store requires its pinned registry"
            )
        self.registry.validate()

    def authorize(
        self,
        *,
        plan: NetworkRequestPlan,
        receipt: SignedAuthorizationReceipt,
        authority: AuthorizationAuthority,
        clock: TrustedClock,
    ) -> NetworkAuthorizationSession:
        trusted_clock = require_trusted_clock(clock)
        if type(plan) is not NetworkRequestPlan:
            raise EvaluationAuthorizationError(
                "network execution requires an exact request plan"
            )
        try:
            plan.validate(registry=self.registry)
        except ContractError as exc:
            raise EvaluationAuthorizationError(
                "network request plan is not registry-bound"
            ) from exc
        if authority.authorization_class != "EXTERNAL_USER_AUTHORITY":
            raise EvaluationAuthorizationError(
                "network execution requires an external signing authority"
            )
        nonce = str(receipt.bindings.get("authorization_nonce", ""))
        required = plan.bindings(nonce=nonce, registry=self.registry)
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
        observed, time_floor_id = _advance_authorization_time_floor(
            root=self.root,
            allowed_root=self.allowed_root,
            network_registry_id=self.registry.registry_id,
            clock=trusted_clock,
        )
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
                    "expires_at": receipt.expires_at,
                    "time_authority": trusted_clock.mode,
                    "time_floor_id": time_floor_id,
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
        object.__setattr__(session, "expires_at", receipt.expires_at)
        object.__setattr__(session, "time_floor_id", time_floor_id)
        with _ISSUED_SESSIONS_LOCK:
            _ISSUED_SESSIONS[session] = {
                "marker_path": marker,
                "marker_sha256": sha256_bytes(marker_payload),
                "next_page": 0,
                "registry": self.registry,
                "time_floor_root": self.root,
                "allowed_root": self.allowed_root,
                "network_registry_id": self.registry.registry_id,
            }
        return session


def network_authorization_request(
    plan: NetworkRequestPlan,
    *,
    registry: NetworkAcquisitionRegistry,
    clock: TrustedClock,
    nonce: str | None = None,
) -> Mapping[str, object]:
    issued = require_trusted_clock(clock).now()
    chosen_nonce = nonce or secrets.token_urlsafe(32)
    if type(plan) is not NetworkRequestPlan:
        raise EvaluationAuthorizationError(
            "network authorization request requires an exact request plan"
        )
    try:
        plan.validate(registry=registry)
    except ContractError as exc:
        raise EvaluationAuthorizationError(
            "network authorization request plan is not registry-bound"
        ) from exc
    bindings = plan.bindings(nonce=chosen_nonce, registry=registry)
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
