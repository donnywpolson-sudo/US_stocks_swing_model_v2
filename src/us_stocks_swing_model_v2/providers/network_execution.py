from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
from typing import Callable, Mapping, TypeVar
from urllib.parse import parse_qsl, urlparse
from weakref import WeakKeyDictionary

from ..clock import TrustedClock, require_trusted_clock
from ..common import canonical_json_bytes, iso_z, sha256_bytes
from ..errors import ContractError, EvaluationAuthorizationError
from .snapshots import NetworkAcquisitionRegistry, normalize_response_headers


def _exact_query_parts(query: str) -> tuple[tuple[str, str, str], ...]:
    raw_parts = query.split("&") if query else []
    try:
        parsed = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise ContractError("network request query is malformed") from exc
    if len(parsed) != len(raw_parts):
        raise ContractError("network request query is not exact")
    keys = [key for key, _ in parsed]
    if len(keys) != len(set(keys)):
        raise ContractError("network request query keys must be unique")
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
            raise ContractError("network request plan requires its pinned registry")
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
            raise ContractError("network request plan fields have invalid types")
        parsed = urlparse(self.initial_url)
        origin_path = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if registry.allowed_origin_paths.get(self.source) != origin_path:
            raise ContractError("network request plan is outside the pinned registry")
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ContractError("network request URL is invalid")
        query_parts = _exact_query_parts(parsed.query)
        if any(key == "page_token" for _, key, _ in query_parts):
            raise ContractError("initial URL cannot contain a page token")
        if not 1 <= self.timeout_seconds <= 30:
            raise ContractError("network request timeout must be in [1,30]")
        if not 1 <= self.max_response_bytes <= 64 * 1024 * 1024:
            raise ContractError("network request response limit is invalid")
        if not 1 <= self.max_pages <= 100:
            raise ContractError("network request page limit must be in [1,100]")
        if self.pagination_parameter not in {"none", "page_token"}:
            raise ContractError("network request pagination parameter is invalid")
        if self.pagination_parameter == "none" and self.max_pages != 1:
            raise ContractError("non-paginated request must allow exactly one page")
        if self.network_registry_id != registry.registry_id:
            raise ContractError("network request plan registry binding differs")
        expected_plan_id = sha256_bytes(canonical_json_bytes(self._unsigned()))
        if self.plan_id != expected_plan_id:
            raise ContractError("network request plan ID differs from its exact fields")

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
            raise ContractError("network request plan requires its pinned registry")
        registry.validate()
        if pagination_parameter is None:
            pagination = "none"
        elif type(pagination_parameter) is str:
            pagination = pagination_parameter
        else:
            raise ContractError("network request pagination parameter is invalid")
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


@dataclass(frozen=True, init=False, eq=False)
class LocalNetworkExecutionSession:
    plan: NetworkRequestPlan
    session_id: str
    started_at: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "local network sessions must be issued from a validated request plan"
        )

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
                "network request exceeds its local execution plan"
            )
        if page_index == 0:
            if url != self.plan.initial_url or expected_page_token is not None:
                raise EvaluationAuthorizationError(
                    "initial network request differs from its local execution plan"
                )
            return
        if self.plan.pagination_parameter != "page_token" or not expected_page_token:
            raise EvaluationAuthorizationError("network pagination is not enabled")
        initial = urlparse(self.plan.initial_url)
        current = urlparse(url)
        if (current.scheme, current.netloc, current.path) != (
            initial.scheme,
            initial.netloc,
            initial.path,
        ):
            raise EvaluationAuthorizationError(
                "paginated request changed origin or path"
            )
        try:
            initial_query = _exact_query_parts(initial.query)
            current_query = _exact_query_parts(current.query)
        except ContractError as exc:
            raise EvaluationAuthorizationError(str(exc)) from exc
        page_tokens = [
            value for _, key, value in current_query if key == "page_token"
        ]
        if page_tokens != [expected_page_token]:
            raise EvaluationAuthorizationError(
                "paginated request token differs from verified response"
            )
        current_base_query = "&".join(
            raw for raw, key, _ in current_query if key != "page_token"
        )
        initial_base_query = "&".join(raw for raw, _, _ in initial_query)
        if current_base_query != initial_base_query:
            raise EvaluationAuthorizationError(
                "paginated request changed base query parameters"
            )


@dataclass(frozen=True, init=False, eq=False)
class NetworkRequestAttempt:
    attempt_id: str
    plan_id: str
    session_id: str
    source: str
    requested_url: str
    timeout_seconds: int
    max_response_bytes: int
    page_index: int
    started_at: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "network request attempts must be issued by local execution preflight"
        )


@dataclass(frozen=True, init=False, eq=False)
class NetworkResponseEvidence:
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


_ISSUED_LOCAL_SESSIONS: WeakKeyDictionary[
    LocalNetworkExecutionSession, dict[str, object]
] = WeakKeyDictionary()
_ISSUED_REQUEST_ATTEMPTS: WeakKeyDictionary[
    NetworkRequestAttempt, dict[str, object]
] = WeakKeyDictionary()
_ISSUED_NETWORK_RESPONSES: WeakKeyDictionary[
    NetworkResponseEvidence, dict[str, object]
] = WeakKeyDictionary()
_ISSUED_SESSIONS_LOCK = threading.Lock()
_COMMIT_RESULT = TypeVar("_COMMIT_RESULT")


def start_local_network_execution(
    plan: NetworkRequestPlan,
    *,
    registry: NetworkAcquisitionRegistry,
    clock: TrustedClock,
) -> LocalNetworkExecutionSession:
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
) -> NetworkRequestAttempt:
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
            "session_id": session.session_id,
            "source": source,
            "requested_url": url,
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
            "page_index": page_index,
            "started_at": session.started_at,
            "attempt_nonce": secrets.token_urlsafe(32),
        }
        attempt = object.__new__(NetworkRequestAttempt)
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


def _bind_network_response(
    attempt: NetworkRequestAttempt,
    *,
    requested_url: str,
    response_url: str,
    http_status: int,
    raw: bytes,
    headers: Mapping[str, str],
) -> NetworkResponseEvidence:
    if type(attempt) is not NetworkRequestAttempt:
        raise EvaluationAuthorizationError(
            "network response lacks a local request attempt"
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
                "network response differs from its request attempt"
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
        response = object.__new__(NetworkResponseEvidence)
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


def _validate_network_response_unlocked(
    response: NetworkResponseEvidence,
    *,
    source: str,
    requested_url: str,
    response_url: str,
    http_status: int,
    raw: bytes,
    headers: Mapping[str, str],
    max_response_bytes: int,
) -> dict[str, object]:
    if type(response) is not NetworkResponseEvidence:
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


def _validate_network_response(
    response: NetworkResponseEvidence,
    **expected: object,
) -> None:
    with _ISSUED_SESSIONS_LOCK:
        _validate_network_response_unlocked(response, **expected)


def _consume_network_response(
    response: NetworkResponseEvidence,
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
    with _ISSUED_SESSIONS_LOCK:
        issued = _validate_network_response_unlocked(
            response,
            source=source,
            requested_url=requested_url,
            response_url=response_url,
            http_status=http_status,
            raw=raw,
            headers=headers,
            max_response_bytes=max_response_bytes,
        )
        issued["consumed"] = True
        return commit()
