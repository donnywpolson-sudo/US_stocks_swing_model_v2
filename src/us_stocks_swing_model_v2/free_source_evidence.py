from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .alpaca_free_bounded import EvidenceClass, PROFILE_ID, load_profile
from .common import (
    atomic_write_new,
    canonical_json_bytes,
    iso_z,
    require_aware_utc,
    require_contained_path,
    require_sha256,
    sha256_bytes,
)
from .errors import ContractError, IntegrityError
from .locking import ExclusiveFileLock
from .providers.snapshots import normalize_response_headers


ADAPTER_VERSION = "alpaca_free_bounded_sources_v1"
REDACTED = "REDACTED"


@dataclass(frozen=True)
class SourceRequestPlan:
    source: str
    provider: str
    endpoint: str
    method: str
    sanitized_url: str
    canonical_query: tuple[tuple[str, str], ...]
    evidence_class: EvidenceClass
    maximum_pages: int
    maximum_response_bytes: int
    timeout_seconds: int
    secret_query_parameter: str | None
    plan_id: str

    def _unsigned(self) -> dict[str, object]:
        return {
            "profile_id": PROFILE_ID,
            "source": self.source,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "method": self.method,
            "sanitized_url": self.sanitized_url,
            "canonical_query": [[key, value] for key, value in self.canonical_query],
            "evidence_class": self.evidence_class.value,
            "maximum_pages": self.maximum_pages,
            "maximum_response_bytes": self.maximum_response_bytes,
            "timeout_seconds": self.timeout_seconds,
            "secret_query_parameter": self.secret_query_parameter,
        }

    def validate(self) -> None:
        if (
            self.method != "GET"
            or type(self.evidence_class) is not EvidenceClass
            or not self.source
            or not self.provider
            or not self.endpoint.startswith("https://")
            or not 1 <= self.maximum_pages <= 100
            or not 1 <= self.maximum_response_bytes <= 64 * 1024 * 1024
            or not 1 <= self.timeout_seconds <= 30
        ):
            raise ContractError("source request plan is outside its bounded contract")
        keys = [key for key, _ in self.canonical_query]
        if len(keys) != len(set(keys)) or any(not key for key in keys):
            raise ContractError("source request canonical query is invalid")
        if self.secret_query_parameter:
            if dict(self.canonical_query).get(self.secret_query_parameter) != REDACTED:
                raise ContractError("secret query parameter is not redacted")
        expected_url = self.endpoint + (
            f"?{urlencode(self.canonical_query)}" if self.canonical_query else ""
        )
        if self.sanitized_url != expected_url:
            raise ContractError("source request sanitized URL differs from canonical query")
        if self.secret_query_parameter and REDACTED not in self.sanitized_url:
            raise ContractError("source request sanitized URL lacks its redaction marker")
        if self.plan_id != sha256_bytes(canonical_json_bytes(self._unsigned())):
            raise IntegrityError("source request plan ID differs from canonical content")

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": PROFILE_ID,
            "plan_id": self.plan_id,
            "source": self.source,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "method": self.method,
            "sanitized_url": self.sanitized_url,
            "canonical_query": [[key, value] for key, value in self.canonical_query],
            "evidence_class": self.evidence_class.value,
            "maximum_pages": self.maximum_pages,
            "maximum_response_bytes": self.maximum_response_bytes,
            "timeout_seconds": self.timeout_seconds,
            "secret_query_parameter": self.secret_query_parameter,
            "network_default": "DISABLED",
            "execution_authorized": False,
        }

    @classmethod
    def create(
        cls,
        *,
        source: str,
        provider: str,
        endpoint: str,
        query: Iterable[tuple[str, str]],
        evidence_class: EvidenceClass,
        maximum_pages: int,
        maximum_response_bytes: int,
        timeout_seconds: int = 30,
        secret_query_parameter: str | None = None,
    ) -> "SourceRequestPlan":
        canonical_query = tuple((str(key), str(value)) for key, value in query)
        keys = [key for key, _ in canonical_query]
        if len(keys) != len(set(keys)):
            raise ContractError("source request query keys must be unique")
        if secret_query_parameter is not None and secret_query_parameter in keys:
            raise ContractError("secret query parameter cannot enter the persisted plan")
        sanitized_query = canonical_query + (
            ((secret_query_parameter, REDACTED),) if secret_query_parameter else ()
        )
        sanitized_url = endpoint + (f"?{urlencode(sanitized_query)}" if sanitized_query else "")
        unsigned = {
            "profile_id": PROFILE_ID,
            "source": source,
            "provider": provider,
            "endpoint": endpoint,
            "method": "GET",
            "sanitized_url": sanitized_url,
            "canonical_query": [[key, value] for key, value in sanitized_query],
            "evidence_class": evidence_class.value,
            "maximum_pages": maximum_pages,
            "maximum_response_bytes": maximum_response_bytes,
            "timeout_seconds": timeout_seconds,
            "secret_query_parameter": secret_query_parameter,
        }
        if (
            not source
            or not provider
            or not endpoint.startswith("https://")
            or not 1 <= maximum_pages <= 100
            or not 1 <= maximum_response_bytes <= 64 * 1024 * 1024
            or not 1 <= timeout_seconds <= 30
        ):
            raise ContractError("source request plan is outside its bounded contract")
        plan = cls(
            source=source,
            provider=provider,
            endpoint=endpoint,
            method="GET",
            sanitized_url=sanitized_url,
            canonical_query=sanitized_query,
            evidence_class=evidence_class,
            maximum_pages=maximum_pages,
            maximum_response_bytes=maximum_response_bytes,
            timeout_seconds=timeout_seconds,
            secret_query_parameter=secret_query_parameter,
            plan_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        plan.validate()
        return plan

    def transport_url(self, *, secret: str | None = None, page_token: str | None = None) -> str:
        self.validate()
        query = list(self.canonical_query)
        if self.secret_query_parameter:
            if not secret:
                if self.provider == "alpha_vantage":
                    raise ContractError("missing credential variable: ALPHA_VANTAGE_API_KEY")
                raise ContractError("request query credential is missing")
            query = [
                (key, secret if key == self.secret_query_parameter else value)
                for key, value in query
            ]
        elif secret is not None:
            raise ContractError("request does not accept a query credential")
        if page_token is not None:
            if not page_token or any(key == "page_token" for key, _ in query):
                raise ContractError("pagination token is invalid or duplicated")
            query.append(("page_token", page_token))
        return self.endpoint + (f"?{urlencode(query)}" if query else "")

    def sanitized_page_url(self, page_token: str | None) -> str:
        self.validate()
        query = list(self.canonical_query)
        if page_token is not None:
            query.append(("page_token", page_token))
        return self.endpoint + (f"?{urlencode(query)}" if query else "")


def alpha_vantage_listing_plan(
    *, repository_root: Path, as_of: date, state: str
) -> SourceRequestPlan:
    profile = load_profile(repository_root)
    if state not in {"active", "delisted"}:
        raise ContractError("Alpha Vantage listing state must be active or delisted")
    candidate = profile["historical_universe_candidate"]
    return SourceRequestPlan.create(
        source="alpha_vantage_listing_status",
        provider="alpha_vantage",
        endpoint=str(candidate["endpoint"]),
        query=(("function", "LISTING_STATUS"), ("date", as_of.isoformat()), ("state", state)),
        evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
        maximum_pages=1,
        maximum_response_bytes=16 * 1024 * 1024,
        secret_query_parameter="apikey",
    )


def alpaca_bars_plan(
    *,
    repository_root: Path,
    symbols: Iterable[str],
    start: date,
    end_exclusive: date,
    evidence_class: EvidenceClass,
) -> SourceRequestPlan:
    profile = load_profile(repository_root)
    canonical = tuple(sorted({str(value).strip().upper() for value in symbols if str(value).strip()}))
    if not canonical or start < date(2016, 1, 1) or end_exclusive <= start:
        raise ContractError("Alpaca bars plan date range or symbols are invalid")
    bars = profile["bars"]
    return SourceRequestPlan.create(
        source="alpaca_free_bounded_bars",
        provider="alpaca",
        endpoint=str(bars["endpoint"]),
        query=(
            ("symbols", ",".join(canonical)),
            ("start", start.isoformat()),
            ("end", end_exclusive.isoformat()),
            ("timeframe", "1Day"),
            ("adjustment", "raw"),
            ("feed", "sip"),
            ("sort", "asc"),
            ("limit", "10000"),
        ),
        evidence_class=evidence_class,
        maximum_pages=int(bars["maximum_pages_per_unit"]),
        maximum_response_bytes=int(bars["maximum_response_bytes_per_page"]),
    )


def prospective_source_plans(
    *, repository_root: Path, observed_for: date
) -> tuple[SourceRequestPlan, ...]:
    profile = load_profile(repository_root)
    identity = profile["prospective_identity"]
    actions = profile["corporate_actions"]
    plans = [
        SourceRequestPlan.create(
            source="alpaca_free_bounded_assets",
            provider="alpaca",
            endpoint=str(identity["alpaca_assets_endpoint"]),
            query=(("asset_class", "us_equity"),),
            evidence_class=EvidenceClass.PROSPECTIVE_AS_OBSERVED,
            maximum_pages=1,
            maximum_response_bytes=32 * 1024 * 1024,
        ),
        SourceRequestPlan.create(
            source="alpaca_free_bounded_corporate_actions",
            provider="alpaca",
            endpoint=str(actions["rest_endpoint"]),
            query=(
                ("start", observed_for.isoformat()),
                ("end", observed_for.isoformat()),
                ("region", "us"),
                ("sort", "asc"),
                ("limit", "1000"),
            ),
            evidence_class=EvidenceClass.PROSPECTIVE_AS_OBSERVED,
            maximum_pages=100,
            maximum_response_bytes=16 * 1024 * 1024,
        ),
    ]
    for item, source in zip(
        identity["nasdaq_files"],
        ("nasdaq_free_bounded_listed", "nasdaq_free_bounded_otherlisted"),
        strict=True,
    ):
        plans.append(
            SourceRequestPlan.create(
                source=source,
                provider="nasdaq_trader",
                endpoint=str(item["url"]),
                query=(),
                evidence_class=EvidenceClass.PROSPECTIVE_AS_OBSERVED,
                maximum_pages=1,
                maximum_response_bytes=16 * 1024 * 1024,
            )
        )
    return tuple(plans)


@dataclass(frozen=True)
class RawEvidenceReceipt:
    receipt_id: str
    logical_request_id: str
    occurrence_number: int
    prior_receipt_id: str | None
    provider: str
    source: str
    endpoint: str
    method: str
    sanitized_url: str
    canonical_query: tuple[tuple[str, str], ...]
    requested_at: datetime
    retrieved_at: datetime
    response_headers: dict[str, str]
    http_status: int
    page_index: int
    requested_page_token: str | None
    next_page_token: str | None
    retry_attempt: int
    provider_request_id: str | None
    source_file_created_at: datetime | None
    raw_sha256: str
    raw_bytes: int
    adapter_version: str
    schema_version: int
    evidence_class: EvidenceClass
    parent_request_id: str | None
    parsing_status: str
    validation_status: str
    provider_error: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "logical_request_id": self.logical_request_id,
            "occurrence_number": self.occurrence_number,
            "prior_receipt_id": self.prior_receipt_id,
            "provider": self.provider,
            "source": self.source,
            "endpoint": self.endpoint,
            "method": self.method,
            "sanitized_url": self.sanitized_url,
            "canonical_query": [[key, value] for key, value in self.canonical_query],
            "requested_at": iso_z(self.requested_at),
            "retrieved_at": iso_z(self.retrieved_at),
            "response_headers": self.response_headers,
            "http_status": self.http_status,
            "page_index": self.page_index,
            "requested_page_token": self.requested_page_token,
            "next_page_token": self.next_page_token,
            "retry_attempt": self.retry_attempt,
            "provider_request_id": self.provider_request_id,
            "source_file_created_at": (
                iso_z(self.source_file_created_at) if self.source_file_created_at else None
            ),
            "raw_sha256": self.raw_sha256,
            "raw_bytes": self.raw_bytes,
            "adapter_version": self.adapter_version,
            "schema_version": self.schema_version,
            "evidence_class": self.evidence_class.value,
            "parent_request_id": self.parent_request_id,
            "parsing_status": self.parsing_status,
            "validation_status": self.validation_status,
            "provider_error": self.provider_error,
        }


class RawEvidenceStore:
    """Append-only occurrence receipts with content-deduplicated raw bytes."""

    def __init__(self, root: Path, *, allowed_root: Path) -> None:
        self.root = Path(root)
        self.allowed_root = Path(allowed_root)
        if not self.root.is_absolute() or not self.allowed_root.is_absolute():
            raise ContractError("raw evidence paths must be absolute")
        require_contained_path(self.root, self.allowed_root, must_exist=False)

    def append(
        self,
        *,
        plan: SourceRequestPlan,
        raw: bytes,
        requested_at: datetime,
        retrieved_at: datetime,
        response_headers: Mapping[str, str],
        http_status: int,
        page_index: int,
        requested_page_token: str | None,
        next_page_token: str | None,
        retry_attempt: int,
        parent_request_id: str | None,
        parsing_status: str,
        validation_status: str,
        source_file_created_at: datetime | None = None,
    ) -> RawEvidenceReceipt:
        plan.validate()
        requested = require_aware_utc(requested_at, "requested_at")
        retrieved = require_aware_utc(retrieved_at, "retrieved_at")
        if retrieved < requested or not raw:
            raise ContractError("raw evidence request chronology or payload is invalid")
        if type(http_status) is not int or not 100 <= http_status <= 599:
            raise ContractError("raw evidence HTTP status is invalid")
        if type(page_index) is not int or not 0 <= page_index < plan.maximum_pages:
            raise ContractError("raw evidence page index is outside the plan")
        if type(retry_attempt) is not int or not 1 <= retry_attempt <= 4:
            raise ContractError("raw evidence retry attempt is outside the policy")
        if parent_request_id is not None:
            require_sha256(parent_request_id, "parent_request_id")
        if requested_page_token is not None and (
            not isinstance(requested_page_token, str) or not requested_page_token
        ):
            raise ContractError("requested page token is invalid")
        if next_page_token is not None and (
            not isinstance(next_page_token, str) or not next_page_token
        ):
            raise ContractError("next page token is invalid")
        if source_file_created_at is not None:
            source_created = require_aware_utc(source_file_created_at, "source_file_created_at")
            if source_created > retrieved:
                raise ContractError("source file creation time cannot follow retrieval")
        else:
            source_created = None
        normalized_headers = normalize_response_headers(response_headers)
        provider_request_id = normalized_headers.get("x-request-id")
        raw_hash = sha256_bytes(raw)
        logical_request_id = sha256_bytes(
            canonical_json_bytes(
                {
                    "plan_id": plan.plan_id,
                    "page_index": page_index,
                    "requested_page_token": requested_page_token,
                    "retry_attempt": retry_attempt,
                }
            )
        )
        source_root = self.root / plan.source
        with ExclusiveFileLock(source_root / ".append.lock", allowed_root=self.allowed_root):
            receipt_directory = source_root / "receipts"
            prior = self._ordered_receipts(receipt_directory)
            prior_receipt = prior[-1] if prior else None
            occurrence = len(prior) + 1
            unsigned = {
                "logical_request_id": logical_request_id,
                "occurrence_number": occurrence,
                "prior_receipt_id": prior_receipt.receipt_id if prior_receipt else None,
                "provider": plan.provider,
                "source": plan.source,
                "endpoint": plan.endpoint,
                "method": plan.method,
                "sanitized_url": plan.sanitized_page_url(requested_page_token),
                "canonical_query": [[key, value] for key, value in plan.canonical_query],
                "requested_at": iso_z(requested),
                "retrieved_at": iso_z(retrieved),
                "response_headers": normalized_headers,
                "http_status": http_status,
                "page_index": page_index,
                "requested_page_token": requested_page_token,
                "next_page_token": next_page_token,
                "retry_attempt": retry_attempt,
                "provider_request_id": provider_request_id,
                "source_file_created_at": iso_z(source_created) if source_created else None,
                "raw_sha256": raw_hash,
                "raw_bytes": len(raw),
                "adapter_version": ADAPTER_VERSION,
                "schema_version": 1,
                "evidence_class": plan.evidence_class.value,
                "parent_request_id": parent_request_id,
                "parsing_status": parsing_status,
                "validation_status": validation_status,
                "provider_error": not 200 <= http_status <= 299,
            }
            receipt_id = sha256_bytes(canonical_json_bytes(unsigned))
            receipt = RawEvidenceReceipt(
                receipt_id=receipt_id,
                logical_request_id=logical_request_id,
                occurrence_number=occurrence,
                prior_receipt_id=unsigned["prior_receipt_id"],
                provider=plan.provider,
                source=plan.source,
                endpoint=plan.endpoint,
                method=plan.method,
                sanitized_url=unsigned["sanitized_url"],
                canonical_query=plan.canonical_query,
                requested_at=requested,
                retrieved_at=retrieved,
                response_headers=normalized_headers,
                http_status=http_status,
                page_index=page_index,
                requested_page_token=requested_page_token,
                next_page_token=next_page_token,
                retry_attempt=retry_attempt,
                provider_request_id=provider_request_id,
                source_file_created_at=source_created,
                raw_sha256=raw_hash,
                raw_bytes=len(raw),
                adapter_version=ADAPTER_VERSION,
                schema_version=1,
                evidence_class=plan.evidence_class,
                parent_request_id=parent_request_id,
                parsing_status=parsing_status,
                validation_status=validation_status,
                provider_error=not 200 <= http_status <= 299,
            )
            raw_path = source_root / "objects" / raw_hash / "raw.bin"
            if not raw_path.exists():
                atomic_write_new(raw_path, raw)
            elif raw_path.read_bytes() != raw:
                raise IntegrityError("raw object hash collision or corruption")
            atomic_write_new(
                receipt_directory / f"{occurrence:08d}-{receipt_id}.json",
                canonical_json_bytes(receipt.as_dict()),
            )
            return receipt

    def _ordered_receipts(self, directory: Path) -> tuple[RawEvidenceReceipt, ...]:
        if not directory.exists():
            return ()
        receipts: list[RawEvidenceReceipt] = []
        prior_id: str | None = None
        for path in sorted(directory.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("prior_receipt_id") != prior_id:
                raise IntegrityError("raw evidence receipt predecessor chain is broken")
            receipt_id = require_sha256(payload.get("receipt_id"), "receipt_id")
            unsigned = {key: value for key, value in payload.items() if key != "receipt_id"}
            if sha256_bytes(canonical_json_bytes(unsigned)) != receipt_id:
                raise IntegrityError("raw evidence receipt ID differs from canonical content")
            if path.name != f"{int(payload['occurrence_number']):08d}-{receipt_id}.json":
                raise IntegrityError("raw evidence receipt filename differs from occurrence content")
            receipts.append(
                RawEvidenceReceipt(
                    receipt_id=receipt_id,
                    logical_request_id=require_sha256(payload["logical_request_id"], "logical_request_id"),
                    occurrence_number=int(payload["occurrence_number"]),
                    prior_receipt_id=payload["prior_receipt_id"],
                    provider=str(payload["provider"]),
                    source=str(payload["source"]),
                    endpoint=str(payload["endpoint"]),
                    method=str(payload["method"]),
                    sanitized_url=str(payload["sanitized_url"]),
                    canonical_query=tuple((str(a), str(b)) for a, b in payload["canonical_query"]),
                    requested_at=datetime.fromisoformat(payload["requested_at"].replace("Z", "+00:00")),
                    retrieved_at=datetime.fromisoformat(payload["retrieved_at"].replace("Z", "+00:00")),
                    response_headers=dict(payload["response_headers"]),
                    http_status=int(payload["http_status"]),
                    page_index=int(payload["page_index"]),
                    requested_page_token=payload["requested_page_token"],
                    next_page_token=payload["next_page_token"],
                    retry_attempt=int(payload["retry_attempt"]),
                    provider_request_id=payload["provider_request_id"],
                    source_file_created_at=(
                        datetime.fromisoformat(payload["source_file_created_at"].replace("Z", "+00:00"))
                        if payload["source_file_created_at"] else None
                    ),
                    raw_sha256=str(payload["raw_sha256"]),
                    raw_bytes=int(payload["raw_bytes"]),
                    adapter_version=str(payload["adapter_version"]),
                    schema_version=int(payload["schema_version"]),
                    evidence_class=EvidenceClass(payload["evidence_class"]),
                    parent_request_id=payload["parent_request_id"],
                    parsing_status=str(payload["parsing_status"]),
                    validation_status=str(payload["validation_status"]),
                    provider_error=bool(payload["provider_error"]),
                )
            )
            prior_id = receipt_id
        return tuple(receipts)

    def validate(self) -> dict[str, object]:
        source_reports: dict[str, object] = {}
        if not self.root.exists():
            return {"state": "EMPTY", "sources": {}, "receipt_count": 0}
        total = 0
        for source_root in sorted(path for path in self.root.iterdir() if path.is_dir()):
            receipts = self._ordered_receipts(source_root / "receipts")
            for receipt in receipts:
                raw_path = source_root / "objects" / receipt.raw_sha256 / "raw.bin"
                if not raw_path.is_file() or sha256_bytes(raw_path.read_bytes()) != receipt.raw_sha256:
                    raise IntegrityError("raw evidence object is missing or corrupt")
            total += len(receipts)
            source_reports[source_root.name] = {"receipt_count": len(receipts)}
        return {"state": "PASS", "sources": source_reports, "receipt_count": total}


def parse_alpha_vantage_listing_csv(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError("Alpha Vantage listing response is not UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"symbol", "name", "exchange", "assetType", "ipoDate", "delistingDate", "status"}
    if reader.fieldnames is None or not required <= set(reader.fieldnames):
        raise ContractError("Alpha Vantage listing CSV schema is not recognized")
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    duplicates = 0
    for row in reader:
        normalized = {str(key): str(value or "") for key, value in row.items()}
        key = (normalized["symbol"], normalized["ipoDate"], normalized["delistingDate"])
        duplicates += key in seen
        seen.add(key)
        rows.append(normalized)
    return {
        "schema_status": "PASS",
        "rows": rows,
        "row_count": len(rows),
        "duplicate_count": duplicates,
        "changed_response_detection": "RAW_HASH_AND_OCCURRENCE_RECEIPT",
        "semantics_status": "CANDIDATE_PENDING_KNOWN_CASE_PROBES",
    }


def parse_nasdaq_symbol_directory(
    raw: bytes,
    *,
    source_name: str,
    retrieved_at: datetime,
) -> dict[str, object]:
    if source_name not in {"nasdaqlisted.txt", "otherlisted.txt"}:
        raise ContractError("Nasdaq directory source name is unsupported")
    retrieved = require_aware_utc(retrieved_at, "retrieved_at")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ContractError("Nasdaq symbol directory must be ASCII") from exc
    lines = [line.rstrip("\r") for line in text.split("\n") if line.rstrip("\r")]
    if len(lines) < 3:
        raise ContractError("Nasdaq symbol directory is incomplete")
    header = lines[0].split("|")
    if len(header) != len(set(header)) or any(not field for field in header):
        raise ContractError("Nasdaq symbol directory header is invalid")
    trailer = lines[-1].split("|", 1)[0]
    prefix = "File Creation Time: "
    if not trailer.startswith(prefix):
        raise ContractError("Nasdaq symbol directory lacks its creation-time trailer")
    try:
        local_created = datetime.strptime(trailer[len(prefix) :], "%m%d%Y%H:%M").replace(
            tzinfo=ZoneInfo("America/New_York")
        )
    except ValueError as exc:
        raise ContractError("Nasdaq file creation time is invalid") from exc
    created = local_created.astimezone(retrieved.tzinfo)
    if created > retrieved:
        raise ContractError("Nasdaq file creation time cannot follow retrieval")
    rows: list[dict[str, str]] = []
    seen_symbols: set[str] = set()
    for line in lines[1:-1]:
        values = line.split("|")
        if len(values) != len(header):
            raise ContractError("Nasdaq directory row width differs from its header")
        row = dict(zip(header, values, strict=True))
        symbol = row.get("Symbol") or row.get("ACT Symbol")
        if not symbol or symbol in seen_symbols:
            raise ContractError("Nasdaq directory symbol is missing or duplicated")
        seen_symbols.add(symbol)
        rows.append(row)
    return {
        "source_name": source_name,
        "file_created_at": iso_z(created),
        "schema_fields": header,
        "row_count": len(rows),
        "rows": rows,
    }


@dataclass(frozen=True)
class ProspectiveAsset:
    provider_asset_id: str
    symbol: str
    exchange: str
    status: str
    tradable: bool
    marginable: bool
    shortable: bool
    borrow_status: str | None
    easy_to_borrow: bool | None
    fractionable: bool
    attributes: tuple[str, ...]
    unknown_fields: Mapping[str, object]


def parse_alpaca_asset_master(raw: bytes) -> tuple[ProspectiveAsset, ...]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("Alpaca asset snapshot is not valid JSON") from exc
    if not isinstance(payload, list):
        raise ContractError("Alpaca complete asset snapshot must be an array")
    known = {
        "id", "class", "exchange", "symbol", "name", "status", "tradable",
        "marginable", "shortable", "borrow_status", "easy_to_borrow",
        "fractionable", "attributes",
    }
    records: list[ProspectiveAsset] = []
    ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ContractError("Alpaca asset row must be an object")
        if item.get("class") != "us_equity":
            continue
        asset_id = item.get("id")
        symbol = item.get("symbol")
        if not isinstance(asset_id, str) or not asset_id or asset_id in ids:
            raise ContractError("Alpaca asset ID is missing or duplicated")
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip()
            or not symbol.isascii()
        ):
            raise ContractError("Alpaca asset symbol is invalid")
        bool_fields = ("tradable", "marginable", "shortable", "fractionable")
        if any(type(item.get(field)) is not bool for field in bool_fields):
            raise ContractError("Alpaca asset eligibility flags must be exact booleans")
        legacy = item.get("easy_to_borrow")
        if legacy is not None and type(legacy) is not bool:
            raise ContractError("legacy easy_to_borrow must be boolean or null")
        borrow = item.get("borrow_status")
        if borrow is not None and not isinstance(borrow, str):
            raise ContractError("borrow_status must be text or null")
        attributes = item.get("attributes", [])
        if not isinstance(attributes, list) or any(not isinstance(value, str) for value in attributes):
            raise ContractError("Alpaca asset attributes must be a text list")
        ids.add(asset_id)
        records.append(
            ProspectiveAsset(
                provider_asset_id=asset_id,
                symbol=symbol,
                exchange=str(item.get("exchange", "")),
                status=str(item.get("status", "")),
                tradable=item["tradable"],
                marginable=item["marginable"],
                shortable=item["shortable"],
                borrow_status=borrow,
                easy_to_borrow=legacy,
                fractionable=item["fractionable"],
                attributes=tuple(attributes),
                unknown_fields={key: value for key, value in item.items() if key not in known},
            )
        )
    return tuple(sorted(records, key=lambda record: record.provider_asset_id))


def parse_corporate_action_groups(raw: bytes, *, known_groups: Iterable[str]) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("corporate-action response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ContractError("corporate-action response root must be an object")
    known = set(known_groups)
    actions: list[dict[str, object]] = []
    unknown: list[dict[str, object]] = []
    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or not token):
        raise ContractError("corporate-action pagination token is invalid")
    if "corporate_actions" in payload:
        grouped = payload["corporate_actions"]
        if not isinstance(grouped, dict):
            raise ContractError("corporate-action envelope is invalid")
        envelope_schema = "NESTED_CORPORATE_ACTIONS"
        unknown_envelope_fields = sorted(
            key for key in payload if key not in {"corporate_actions", "next_page_token"}
        )
    else:
        grouped = {key: value for key, value in payload.items() if key != "next_page_token"}
        envelope_schema = "FLAT_GROUPS"
        unknown_envelope_fields = []
    for group, values in grouped.items():
        if not isinstance(values, list):
            raise ContractError("corporate-action group must be an array")
        destination = actions if group in known else unknown
        for value in values:
            if not isinstance(value, dict):
                raise ContractError("corporate-action row must be an object")
            destination.append({"group": group if group in known else "UNKNOWN", "source_group": group, "raw": value})
    return {
        "known_actions": actions,
        "unknown_actions": unknown,
        "next_page_token": token,
        "terminal_page": token is None,
        "envelope_schema": envelope_schema,
        "unknown_envelope_fields": unknown_envelope_fields,
    }


def validate_complete_pagination(receipts: Iterable[RawEvidenceReceipt]) -> None:
    ordered = tuple(receipts)
    if not ordered:
        raise ContractError("pagination evidence is empty")
    expected_token: str | None = None
    seen: set[str] = set()
    for index, receipt in enumerate(ordered):
        if receipt.page_index != index or receipt.requested_page_token != expected_token:
            raise IntegrityError("pagination request lineage is incomplete or out of order")
        token = receipt.next_page_token
        if token is not None:
            if token in seen:
                raise IntegrityError("pagination token repeated")
            seen.add(token)
        expected_token = token
    if expected_token is not None:
        raise IntegrityError("pagination did not reach a terminal page")


def validate_accepted_bars_receipts(receipts: Iterable[RawEvidenceReceipt]) -> dict[str, object]:
    ordered = tuple(receipts)
    validate_complete_pagination(ordered)
    evidence_classes: set[str] = set()
    for receipt in ordered:
        query = dict(receipt.canonical_query)
        if (
            receipt.source != "alpaca_free_bounded_bars"
            or receipt.provider != "alpaca"
            or receipt.http_status != 200
            or receipt.parsing_status != "PARSED"
            or not receipt.validation_status.startswith("PASS")
            or query.get("feed") != "sip"
            or query.get("timeframe") != "1Day"
            or query.get("adjustment") != "raw"
            or query.get("sort") != "asc"
            or "asof" in query
        ):
            raise IntegrityError("accepted bars receipt violates the explicit SIP/raw contract")
        if "iex" in receipt.sanitized_url.lower():
            raise IntegrityError("accepted bars receipts mix or reference IEX")
        evidence_classes.add(receipt.evidence_class.value)
    if len(evidence_classes) != 1:
        raise IntegrityError("accepted bars receipt set mixes evidence classes")
    return {
        "state": "PASS",
        "feed": "sip",
        "adjustment": "raw",
        "terminal_page": True,
        "receipt_count": len(ordered),
        "evidence_class": next(iter(evidence_classes)),
    }
