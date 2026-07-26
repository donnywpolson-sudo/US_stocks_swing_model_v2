from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from ..common import (
    assert_exact_tree,
    atomic_write,
    canonical_json_bytes,
    iso_z,
    reject_link,
    require_aware_utc,
    require_contained_path,
    require_sha256,
    sha256_bytes,
)
from ..errors import ContractError, IntegrityError
from ..capabilities import SyntheticOnlyPermit, require_synthetic_permit
from ..clock import TrustedClock, require_trusted_clock
from ..locking import ExclusiveFileLock


SAFE_SOURCE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
SNAPSHOT_FILES = {"raw.bin", "headers.json", "receipt.json"}
RECEIPT_FIELDS = {
    "schema_version",
    "source",
    "url",
    "http_status",
    "retrieved_at",
    "raw_sha256",
    "raw_bytes",
    "headers",
    "acquisition_mode",
    "acquisition_capability_id",
    "time_authority",
    "synthetic_permit_id",
    "snapshot_id",
}
ALLOWED_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-length",
        "content-type",
        "date",
        "etag",
        "last-modified",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "x-request-id",
    }
)


def normalize_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ContractError("response headers must be explicit strings")
        lowered = key.lower()
        if lowered not in ALLOWED_RESPONSE_HEADERS:
            raise ContractError(f"response header is outside the evidence allowlist: {lowered}")
        if lowered in normalized or "\r" in value or "\n" in value:
            raise ContractError("response headers contain duplicates or control characters")
        normalized[lowered] = value
    return dict(sorted(normalized.items()))


@dataclass(frozen=True, init=False)
class NetworkAcquisitionRegistry:
    registry_id: str
    registry_path: str
    allowed_origin_paths: Mapping[str, str]
    accepted_http_statuses: Mapping[str, tuple[int, ...]]

    @classmethod
    def load(cls, registry_path: Path) -> "NetworkAcquisitionRegistry":
        path = Path(registry_path).resolve(strict=True)
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ContractError("network acquisition registry must be an independent plain file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("network acquisition registry is unreadable") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "project",
            "status",
            "allowed_sources",
        }:
            raise ContractError("network acquisition registry fields differ")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or payload["project"] != "US_stocks_swing_model_v2"
            or payload["status"] != "ACTIVE"
            or not isinstance(payload["allowed_sources"], dict)
        ):
            raise ContractError("network acquisition registry is not active for this project")
        allowed: dict[str, str] = {}
        statuses: dict[str, tuple[int, ...]] = {}
        for source, policy in payload["allowed_sources"].items():
            if not isinstance(source, str) or not SAFE_SOURCE.fullmatch(source):
                raise ContractError("network acquisition registry source is invalid")
            if not isinstance(policy, dict) or set(policy) != {
                "origin_path",
                "accepted_http_statuses",
            }:
                raise ContractError("network acquisition registry source policy is invalid")
            origin_path = policy["origin_path"]
            parsed = urlparse(origin_path) if isinstance(origin_path, str) else None
            if (
                parsed is None
                or parsed.scheme != "https"
                or not parsed.netloc
                or not parsed.path
                or parsed.query
            ):
                raise ContractError("network acquisition registry origin/path is invalid")
            accepted_statuses = policy["accepted_http_statuses"]
            if (
                not isinstance(accepted_statuses, list)
                or not accepted_statuses
                or any(
                    type(status) is not int or not 200 <= status <= 299
                    for status in accepted_statuses
                )
                or accepted_statuses != sorted(set(accepted_statuses))
            ):
                raise ContractError(
                    "network acquisition accepted HTTP statuses are invalid"
                )
            allowed[source] = origin_path
            statuses[source] = tuple(accepted_statuses)
        if not allowed:
            raise ContractError("network acquisition registry has no allowed sources")
        registry = object.__new__(cls)
        object.__setattr__(registry, "registry_id", sha256_bytes(canonical_json_bytes(payload)))
        object.__setattr__(registry, "registry_path", str(path))
        object.__setattr__(registry, "allowed_origin_paths", dict(sorted(allowed.items())))
        object.__setattr__(
            registry,
            "accepted_http_statuses",
            dict(sorted(statuses.items())),
        )
        registry.validate()
        return registry

    def validate(self) -> None:
        path = Path(self.registry_path)
        reject_link(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("network acquisition registry changed or disappeared") from exc
        if sha256_bytes(canonical_json_bytes(payload)) != self.registry_id:
            raise ContractError("network acquisition registry changed after loading")
        require_sha256(self.registry_id, "network_acquisition.registry_id")

    def _issue_response_capability(
        self,
        *,
        source: str,
        requested_url: str,
        response_url: str,
        http_status: int,
        raw: bytes,
        headers: Mapping[str, str],
    ) -> "NetworkAcquisitionCapability":
        self.validate()
        parsed = urlparse(requested_url)
        origin_path = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if self.allowed_origin_paths.get(source) != origin_path:
            raise ContractError("network request source/origin/path is not registry-approved")
        if http_status not in self.accepted_http_statuses.get(source, ()):
            raise ContractError(
                "network response status is not approved for this source"
            )
        normalized_headers = normalize_response_headers(headers)
        unsigned = {
            "registry_id": self.registry_id,
            "registry_path": self.registry_path,
            "source": source,
            "requested_url": requested_url,
            "approved_origin_path": origin_path,
            "response_url": response_url,
            "http_status": http_status,
            "raw_sha256": sha256_bytes(raw),
            "headers_sha256": sha256_bytes(canonical_json_bytes(normalized_headers)),
        }
        capability = object.__new__(NetworkAcquisitionCapability)
        fields = {
            **unsigned,
            "capability_id": sha256_bytes(canonical_json_bytes(unsigned)),
        }
        for name, value in fields.items():
            object.__setattr__(capability, name, value)
        capability.validate(registry=self)
        return capability


@dataclass(frozen=True, init=False)
class NetworkAcquisitionCapability:
    registry_id: str
    registry_path: str
    source: str
    requested_url: str
    approved_origin_path: str
    response_url: str
    http_status: int
    raw_sha256: str
    headers_sha256: str
    capability_id: str

    def validate(self, *, registry: NetworkAcquisitionRegistry) -> None:
        registry.validate()
        parsed = urlparse(self.requested_url)
        unsigned = {
            "registry_id": self.registry_id,
            "registry_path": self.registry_path,
            "source": self.source,
            "requested_url": self.requested_url,
            "approved_origin_path": self.approved_origin_path,
            "response_url": self.response_url,
            "http_status": self.http_status,
            "raw_sha256": self.raw_sha256,
            "headers_sha256": self.headers_sha256,
        }
        if (
            self.registry_id != registry.registry_id
            or self.registry_path != registry.registry_path
            or not SAFE_SOURCE.fullmatch(self.source)
            or parsed.scheme != "https"
            or f"{parsed.scheme}://{parsed.netloc}{parsed.path}" != self.approved_origin_path
            or registry.allowed_origin_paths.get(self.source) != self.approved_origin_path
            or self.http_status
            not in registry.accepted_http_statuses.get(self.source, ())
            or self.response_url != self.requested_url
            or isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
            or self.capability_id != sha256_bytes(canonical_json_bytes(unsigned))
        ):
            raise ContractError("network acquisition capability differs from its exact request")
        require_sha256(self.raw_sha256, "network_capability.raw_sha256")
        require_sha256(self.headers_sha256, "network_capability.headers_sha256")
        require_sha256(self.capability_id, "network_capability.capability_id")


@dataclass(frozen=True)
class LandedSnapshot:
    root: Path
    store_root: Path
    allowed_root: Path
    snapshot_id: str
    source: str
    url: str
    http_status: int
    retrieved_at: datetime
    raw_sha256: str
    headers: Mapping[str, str]
    acquisition_mode: str
    acquisition_capability_id: str
    time_authority: str
    synthetic_permit_id: str | None
    acquisition_registry: NetworkAcquisitionRegistry | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def trust_eligible(self) -> bool:
        # The current receipt is self-hashed, not independently authenticated.
        # Preserve it as acquisition evidence but never promote it to trust.
        return False

    @property
    def raw_path(self) -> Path:
        """Location only. Parsers must call read_verified_bytes()."""
        return self.root / "raw.bin"

    def read_verified_bytes(self) -> bytes:
        loaded, raw = _load_snapshot(
            self.root,
            store_root=self.store_root,
            allowed_root=self.allowed_root,
            allow_pending=False,
            acquisition_registry=self.acquisition_registry,
        )
        if loaded != self:
            raise IntegrityError("snapshot receipt changed after it was loaded")
        return raw


class AsReceivedSnapshotStore:
    """Atomically land immutable raw bytes and receipt metadata before parsing."""

    def __init__(
        self,
        root: Path,
        *,
        allowed_root: Path,
        acquisition_registry: NetworkAcquisitionRegistry | None = None,
    ):
        self.root = Path(root)
        self.allowed_root = Path(allowed_root)
        self.acquisition_registry = acquisition_registry
        if not self.root.is_absolute() or not self.allowed_root.is_absolute():
            raise ContractError("snapshot store and approved root must be absolute")
        require_contained_path(self.allowed_root, self.allowed_root)
        require_contained_path(self.root, self.allowed_root, must_exist=False)

    def land(
        self,
        *,
        source: str,
        url: str,
        http_status: int,
        raw: bytes,
        headers: Mapping[str, str],
        retrieved_at: datetime,
        synthetic_permit: SyntheticOnlyPermit,
        max_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> LandedSnapshot:
        permit = require_synthetic_permit(
            synthetic_permit,
            scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
        )
        return self._land(
            source=source,
            url=url,
            http_status=http_status,
            raw=raw,
            headers=headers,
            retrieved_at=retrieved_at,
            acquisition_mode="SYNTHETIC_DIRECT_NOT_AS_RECEIVED",
            acquisition_capability_id=permit.permit_id,
            time_authority="SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
            synthetic_permit_id=permit.permit_id,
            max_bytes=max_bytes,
        )

    def _land_network_response(
        self,
        *,
        source: str,
        requested_url: str,
        response_url: str,
        http_status: int,
        raw: bytes,
        headers: Mapping[str, str],
        clock: TrustedClock,
        max_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> LandedSnapshot:
        if self.acquisition_registry is None:
            raise ContractError("network landing requires a loader-pinned acquisition registry")
        capability = self.acquisition_registry._issue_response_capability(
            source=source,
            requested_url=requested_url,
            response_url=response_url,
            http_status=http_status,
            raw=raw,
            headers=headers,
        )
        capability.validate(registry=self.acquisition_registry)
        trusted_clock = require_trusted_clock(clock)
        if not trusted_clock.trust_eligible:
            raise ContractError("network as-received landing requires the production UTC clock")
        if (
            capability.source != source
            or capability.requested_url != requested_url
            or response_url != requested_url
            or capability.response_url != response_url
            or capability.http_status != http_status
            or capability.raw_sha256 != sha256_bytes(raw)
            or capability.headers_sha256
            != sha256_bytes(canonical_json_bytes(normalize_response_headers(headers)))
        ):
            raise ContractError("network response redirected or differs from its acquisition capability")
        return self._land(
            source=source,
            url=requested_url,
            http_status=http_status,
            raw=raw,
            headers=headers,
            retrieved_at=trusted_clock.now(),
            acquisition_mode="NETWORK_AS_RECEIVED",
            acquisition_capability_id=capability.capability_id,
            time_authority=trusted_clock.mode,
            synthetic_permit_id=None,
            max_bytes=max_bytes,
        )

    def _land(
        self,
        *,
        source: str,
        url: str,
        http_status: int,
        raw: bytes,
        headers: Mapping[str, str],
        retrieved_at: datetime,
        acquisition_mode: str,
        acquisition_capability_id: str,
        time_authority: str,
        synthetic_permit_id: str | None,
        max_bytes: int,
    ) -> LandedSnapshot:
        if not SAFE_SOURCE.fullmatch(source):
            raise ContractError("snapshot source must be a safe identifier")
        if isinstance(http_status, bool) or not isinstance(http_status, int) or not 100 <= http_status <= 599:
            raise ContractError("snapshot HTTP status must be an integer in [100,599]")
        retrieved = require_aware_utc(retrieved_at, "retrieved_at")
        if not 1 <= len(raw) <= max_bytes <= MAX_SNAPSHOT_BYTES:
            raise ContractError("snapshot response is empty, oversized, or has an invalid byte bound")
        normalized_headers = normalize_response_headers(headers)
        raw_hash = sha256_bytes(raw)
        unsigned = {
            "schema_version": 1,
            "source": source,
            "url": url,
            "http_status": http_status,
            "retrieved_at": iso_z(retrieved),
            "raw_sha256": raw_hash,
            "raw_bytes": len(raw),
            "headers": normalized_headers,
            "acquisition_mode": acquisition_mode,
            "acquisition_capability_id": acquisition_capability_id,
            "time_authority": time_authority,
            "synthetic_permit_id": synthetic_permit_id,
        }
        snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
        source_root = self.root / source
        final = source_root / snapshot_id
        require_contained_path(source_root, self.allowed_root, must_exist=False)
        require_contained_path(final, self.allowed_root, must_exist=False)
        with ExclusiveFileLock(
            self.root / ".locks" / f"{source}.lock",
            allowed_root=self.allowed_root,
        ):
            if final.exists():
                return self.load(final)
            source_root.mkdir(parents=True, exist_ok=True)
            require_contained_path(source_root, self.allowed_root)
            staging = source_root / f".pending-{snapshot_id[:12]}-{uuid.uuid4().hex[:8]}"
            require_contained_path(staging, self.allowed_root, must_exist=False)
            staging.mkdir()
            atomic_write(staging / "raw.bin", raw)
            atomic_write(staging / "headers.json", canonical_json_bytes(normalized_headers))
            atomic_write(
                staging / "receipt.json",
                canonical_json_bytes({**unsigned, "snapshot_id": snapshot_id}),
            )
            loaded, _ = _load_snapshot(
                staging,
                store_root=self.root,
                allowed_root=self.allowed_root,
                allow_pending=True,
                acquisition_registry=(
                    self.acquisition_registry
                    if acquisition_mode == "NETWORK_AS_RECEIVED"
                    else None
                ),
            )
            if loaded.snapshot_id != snapshot_id:
                raise IntegrityError("staged snapshot identity mismatch")
            os.replace(staging, final)
            return self.load(final)

    def load(self, snapshot_dir: Path) -> LandedSnapshot:
        loaded, _ = _load_snapshot(
            Path(snapshot_dir),
            store_root=self.root,
            allowed_root=self.allowed_root,
            allow_pending=False,
            acquisition_registry=self.acquisition_registry,
        )
        return loaded


def _load_snapshot(
    directory: Path,
    *,
    store_root: Path,
    allowed_root: Path,
    allow_pending: bool,
    acquisition_registry: NetworkAcquisitionRegistry | None,
) -> tuple[LandedSnapshot, bytes]:
    directory = require_contained_path(Path(directory), allowed_root)
    store_root = require_contained_path(Path(store_root), allowed_root)
    reject_link(directory)
    try:
        relative = directory.relative_to(store_root)
    except ValueError as exc:
        raise ContractError("snapshot directory is outside the configured store") from exc
    if len(relative.parts) != 2 or not SAFE_SOURCE.fullmatch(relative.parts[0]):
        raise ContractError("snapshot directory must be store/source/snapshot_id")
    if allow_pending:
        if not relative.parts[1].startswith(".pending-"):
            raise IntegrityError("staged snapshot path is not pending")
    else:
        try:
            require_sha256(relative.parts[1], "snapshot.directory_id")
        except ContractError as exc:
            raise IntegrityError("accepted snapshot path is not content addressed") from exc
    try:
        assert_exact_tree(directory, SNAPSHOT_FILES, set())
    except ContractError as exc:
        raise IntegrityError(str(exc)) from exc
    for name in SNAPSHOT_FILES:
        candidate = require_contained_path(directory / name, allowed_root)
        reject_link(candidate)
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise IntegrityError(f"snapshot part is absent or linked: {name}")
    try:
        receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
        headers = json.loads((directory / "headers.json").read_text(encoding="utf-8"))
        raw = (directory / "raw.bin").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("snapshot receipt/raw bytes are invalid") from exc
    if set(receipt) != RECEIPT_FIELDS or not isinstance(headers, dict):
        raise IntegrityError("snapshot receipt fields differ from the exact contract")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise IntegrityError("snapshot schema_version must be integer one")
    if type(receipt["http_status"]) is not int or type(receipt["raw_bytes"]) is not int:
        raise IntegrityError("snapshot numeric receipt fields must be exact integers")
    try:
        normalized_headers = normalize_response_headers(headers)
        require_sha256(receipt["raw_sha256"], "snapshot.raw_sha256")
        require_sha256(receipt["snapshot_id"], "snapshot.snapshot_id")
        require_sha256(receipt["acquisition_capability_id"], "snapshot.acquisition_capability_id")
    except ContractError as exc:
        raise IntegrityError(str(exc)) from exc
    if normalized_headers != headers:
        raise IntegrityError("snapshot headers are not canonical allowlisted evidence")
    raw_hash = sha256_bytes(raw)
    if raw_hash != receipt["raw_sha256"] or len(raw) != receipt["raw_bytes"]:
        raise IntegrityError("snapshot raw bytes differ from receipt")
    unsigned = {
        key: receipt[key]
        for key in (
            "schema_version",
            "source",
            "url",
            "http_status",
            "retrieved_at",
            "raw_sha256",
            "raw_bytes",
            "headers",
            "acquisition_mode",
            "acquisition_capability_id",
            "time_authority",
            "synthetic_permit_id",
        )
    }
    expected = sha256_bytes(canonical_json_bytes(unsigned))
    if receipt["snapshot_id"] != expected:
        raise IntegrityError("snapshot ID differs from receipt")
    if not allow_pending and directory.name != expected:
        raise IntegrityError("snapshot directory name differs from content ID")
    if headers != receipt["headers"] or receipt["source"] != relative.parts[0]:
        raise IntegrityError("snapshot headers/source differ from receipt")
    acquisition_mode = str(receipt["acquisition_mode"])
    time_authority = str(receipt["time_authority"])
    synthetic_permit_id = receipt["synthetic_permit_id"]
    if acquisition_mode == "NETWORK_AS_RECEIVED":
        if time_authority != "PRODUCTION_SYSTEM_UTC" or synthetic_permit_id is not None:
            raise IntegrityError("network snapshot time/acquisition provenance is invalid")
        if acquisition_registry is None:
            raise IntegrityError(
                "network snapshot requires its pinned acquisition registry"
            )
        try:
            capability = acquisition_registry._issue_response_capability(
                source=str(receipt["source"]),
                requested_url=str(receipt["url"]),
                response_url=str(receipt["url"]),
                http_status=int(receipt["http_status"]),
                raw=raw,
                headers=headers,
            )
        except ContractError as exc:
            raise IntegrityError(
                "network snapshot capability cannot be revalidated"
            ) from exc
        if capability.capability_id != receipt["acquisition_capability_id"]:
            raise IntegrityError(
                "network snapshot capability differs from the pinned registry"
            )
    elif acquisition_mode == "SYNTHETIC_DIRECT_NOT_AS_RECEIVED":
        if (
            time_authority != "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE"
            or not isinstance(synthetic_permit_id, str)
        ):
            raise IntegrityError("synthetic snapshot permit/time provenance is invalid")
        try:
            require_sha256(synthetic_permit_id, "snapshot.synthetic_permit_id")
        except ContractError as exc:
            raise IntegrityError("synthetic snapshot permit/time provenance is invalid") from exc
    else:
        raise IntegrityError("snapshot acquisition mode is invalid")
    try:
        retrieved_at = require_aware_utc(
            datetime.fromisoformat(str(receipt["retrieved_at"]).replace("Z", "+00:00")),
            "retrieved_at",
        )
    except (TypeError, ValueError) as exc:
        raise IntegrityError("snapshot retrieval time is invalid") from exc
    loaded = LandedSnapshot(
        root=directory,
        store_root=store_root,
        allowed_root=allowed_root,
        snapshot_id=expected,
        source=str(receipt["source"]),
        url=str(receipt["url"]),
        http_status=int(receipt["http_status"]),
        retrieved_at=retrieved_at,
        raw_sha256=raw_hash,
        headers={str(key): str(value) for key, value in headers.items()},
        acquisition_mode=acquisition_mode,
        acquisition_capability_id=str(receipt["acquisition_capability_id"]),
        time_authority=time_authority,
        synthetic_permit_id=(str(synthetic_permit_id) if synthetic_permit_id is not None else None),
        acquisition_registry=(
            acquisition_registry
            if acquisition_mode == "NETWORK_AS_RECEIVED"
            else None
        ),
    )
    return loaded, raw
