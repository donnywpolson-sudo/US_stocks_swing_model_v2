"""Synthetic-only controls supporting independent audit evidence.

These helpers perform no discovery, provider access, research execution, or
publication. Callers must supply complete, predeclared inputs and retain any
real audit receipt under a separate authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Mapping

from .common import (
    canonical_json_bytes,
    reject_link,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
)
from .errors import ContractError


AUDIT_SURFACES = (
    "git",
    "logs",
    "reports",
    "caches",
    "artifacts",
    "admitted_evidence",
)
SEVERITY_PRIORITIES = {
    "Critical": "P0",
    "High": "P1",
    "Medium": "P2",
    "Low": "P3",
}
TRACEABILITY_DISPOSITIONS = {"CLOSED", "OPEN", "MISSING_EVIDENCE"}
MUTATION_RESULTS = {"FAIL_CLOSED", "FALSE_PASS", "NOT_RUN"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FORBIDDEN_SECRET_FILENAMES = {
    ".env",
    "api.env",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
}
_SECRET_PATTERNS = (
    (
        "PRIVATE_KEY_BLOCK",
        re.compile(
            rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    ("AWS_ACCESS_KEY", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    (
        "BEARER_TOKEN",
        re.compile(rb"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    ),
    (
        "CREDENTIAL_ASSIGNMENT",
        re.compile(
            rb"\b(?:APCA_API_KEY_ID|APCA_API_SECRET_KEY|API_SECRET|ACCESS_TOKEN)"
            rb"[ \t]*[:=][ \t]*[\"']?[A-Za-z0-9._~+/=-]{8,}",
            re.IGNORECASE,
        ),
    ),
)


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ContractError(f"{field} must be a bounded ASCII identifier")
    return value


def _unique_identifiers(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ContractError(f"{field} must be an exact tuple")
    checked = tuple(_identifier(value, f"{field}[{index}]") for index, value in enumerate(values))
    if len(set(checked)) != len(checked):
        raise ContractError(f"{field} must be unique")
    return checked


def _bounded_text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 512
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ContractError(f"{field} must be bounded single-line text")
    return value


@dataclass(frozen=True)
class SecretFinding:
    surface: str
    relative_path: str
    category: str
    line_number: int | None
    byte_count: int
    file_sha256: str | None
    finding_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "relative_path": self.relative_path,
            "category": self.category,
            "line_number": self.line_number,
            "byte_count": self.byte_count,
            "file_sha256": self.file_sha256,
        }

    def validate(self) -> None:
        if self.surface not in AUDIT_SURFACES:
            raise ContractError("secret finding surface is invalid")
        safe_relative_path(self.relative_path)
        _identifier(self.category, "secret_finding.category")
        if self.line_number is not None and (
            type(self.line_number) is not int or self.line_number < 1
        ):
            raise ContractError("secret finding line number is invalid")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ContractError("secret finding byte count is invalid")
        if self.file_sha256 is not None:
            require_sha256(self.file_sha256, "secret_finding.file_sha256")
        if self.category == "FORBIDDEN_SECRET_FILENAME":
            if self.line_number is not None or self.file_sha256 is not None:
                raise ContractError("forbidden secret filenames must not expose file content")
        elif self.line_number is None or self.file_sha256 is None:
            raise ContractError("content findings require redacted line and file identities")
        require_sha256(self.finding_id, "secret_finding.finding_id")
        if self.finding_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("secret finding ID differs from its content")


@dataclass(frozen=True)
class SurfaceRootEvidence:
    surface: str
    relative_path: str
    state: str
    file_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "relative_path": self.relative_path,
            "state": self.state,
            "file_count": self.file_count,
        }

    def validate(self) -> None:
        if self.surface not in AUDIT_SURFACES:
            raise ContractError("audit surface root is invalid")
        safe_relative_path(self.relative_path)
        if self.state not in {"ABSENT", "EMPTY_DIRECTORY", "NONEMPTY_DIRECTORY"}:
            raise ContractError("audit surface root state is invalid")
        if type(self.file_count) is not int or self.file_count < 0:
            raise ContractError("audit surface root file count is invalid")
        if (self.state == "NONEMPTY_DIRECTORY") != (self.file_count > 0):
            raise ContractError("audit surface root state differs from its file count")


@dataclass(frozen=True)
class SurfaceFileEvidence:
    surface: str
    relative_path: str
    byte_count: int
    file_sha256: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "relative_path": self.relative_path,
            "byte_count": self.byte_count,
            "file_sha256": self.file_sha256,
        }

    def validate(self) -> None:
        if self.surface not in AUDIT_SURFACES:
            raise ContractError("audit surface file is invalid")
        safe_relative_path(self.relative_path)
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ContractError("audit surface file byte count is invalid")
        if self.file_sha256 is None:
            lowered = PurePosixPath(self.relative_path).name.lower()
            if lowered not in _FORBIDDEN_SECRET_FILENAMES and not lowered.endswith(".env"):
                raise ContractError("ordinary audit surface files require content hashes")
        else:
            require_sha256(self.file_sha256, "audit_surface_file.file_sha256")


@dataclass(frozen=True)
class SecretScanResult:
    surface_counts: tuple[tuple[str, int], ...]
    surface_roots: tuple[SurfaceRootEvidence, ...]
    files: tuple[SurfaceFileEvidence, ...]
    findings: tuple[SecretFinding, ...]
    passed: bool
    mechanics_only: bool
    scan_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "surface_counts": [
                {"surface": surface, "file_count": count}
                for surface, count in self.surface_counts
            ],
            "surface_roots": [root.as_dict() for root in self.surface_roots],
            "files": [file.as_dict() for file in self.files],
            "findings": [finding.unsigned_dict() | {"finding_id": finding.finding_id} for finding in self.findings],
            "passed": self.passed,
            "mechanics_only": self.mechanics_only,
        }

    def validate(self) -> None:
        if tuple(surface for surface, _ in self.surface_counts) != AUDIT_SURFACES:
            raise ContractError("secret scan does not cover every required surface")
        if any(type(count) is not int or count < 0 for _, count in self.surface_counts):
            raise ContractError("secret scan surface counts must be nonnegative integers")
        for root in self.surface_roots:
            if type(root) is not SurfaceRootEvidence:
                raise ContractError("audit surface roots must be exact values")
            root.validate()
        root_pairs = [
            (root.surface, root.relative_path) for root in self.surface_roots
        ]
        if len(set(root_pairs)) != len(root_pairs):
            raise ContractError("audit surface roots must be unique")
        roots_by_surface = {
            surface: tuple(
                root
                for root in self.surface_roots
                if root.surface == surface
            )
            for surface in AUDIT_SURFACES
        }
        if any(not roots_by_surface[surface] for surface in AUDIT_SURFACES):
            raise ContractError("every audit surface requires an exact root census")
        file_keys: list[tuple[str, str]] = []
        files_by_surface = {surface: [] for surface in AUDIT_SURFACES}
        for file in self.files:
            if type(file) is not SurfaceFileEvidence:
                raise ContractError("audit surface files must be exact values")
            file.validate()
            key = (file.surface, file.relative_path)
            file_keys.append(key)
            files_by_surface[file.surface].append(file)
        expected_file_keys = sorted(
            file_keys,
            key=lambda value: (AUDIT_SURFACES.index(value[0]), value[1]),
        )
        if file_keys != expected_file_keys or len(set(file_keys)) != len(file_keys):
            raise ContractError("audit surface file census must be sorted and unique")
        for surface, count in self.surface_counts:
            if count != len(files_by_surface[surface]):
                raise ContractError("audit surface count differs from its file census")
            root_total = sum(root.file_count for root in roots_by_surface[surface])
            if root_total != count:
                raise ContractError("audit surface root census differs from its files")
        for finding in self.findings:
            if type(finding) is not SecretFinding:
                raise ContractError("secret scan findings must be exact SecretFinding values")
            finding.validate()
        if type(self.passed) is not bool or self.passed is (len(self.findings) != 0):
            raise ContractError("secret scan pass state differs from its findings")
        if self.mechanics_only is not True:
            raise ContractError("secret scan result must remain mechanics-only")
        require_sha256(self.scan_id, "secret_scan.scan_id")
        if self.scan_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("secret scan ID differs from its content")


def scan_declared_audit_surfaces(
    root: Path,
    surfaces: Mapping[str, tuple[str, ...]],
    *,
    surface_roots: Mapping[str, tuple[str, ...]],
    maximum_file_bytes: int = 33_554_432,
) -> SecretScanResult:
    """Discover and scan the exact files below each frozen surface-root census."""

    base = Path(root)
    if not base.is_absolute():
        raise ContractError("secret scan root must be absolute")
    base = require_contained_path(base, base)
    if type(surfaces) is not dict or tuple(surfaces) != AUDIT_SURFACES:
        raise ContractError("secret scan surfaces must use the exact required order")
    if (
        type(surface_roots) is not dict
        or tuple(surface_roots) != AUDIT_SURFACES
    ):
        raise ContractError(
            "surface roots must use the exact required surface order"
        )
    if type(maximum_file_bytes) is not int or maximum_file_bytes <= 0:
        raise ContractError("secret scan maximum_file_bytes must be a positive integer")

    findings: list[SecretFinding] = []
    counts: list[tuple[str, int]] = []
    root_evidence: list[SurfaceRootEvidence] = []
    file_evidence: list[SurfaceFileEvidence] = []
    seen: set[str] = set()
    seen_roots: list[tuple[str, ...]] = []
    for surface in AUDIT_SURFACES:
        raw_relative_paths = surfaces[surface]
        raw_roots = surface_roots[surface]
        if type(raw_relative_paths) is not tuple or type(raw_roots) is not tuple:
            raise ContractError("secret scan files and roots must be exact tuples")
        relative_paths = tuple(
            safe_relative_path(value).as_posix() for value in raw_relative_paths
        )
        if relative_paths != tuple(sorted(set(relative_paths))):
            raise ContractError("declared surface file census must be sorted and unique")
        if not raw_roots:
            raise ContractError("every audit surface requires at least one declared root")

        discovered: set[str] = set()
        for raw_root in raw_roots:
            relative_root = safe_relative_path(raw_root).as_posix()
            root_parts = tuple(PurePosixPath(relative_root).parts)
            if any(
                root_parts[: len(prior)] == prior
                or prior[: len(root_parts)] == root_parts
                for prior in seen_roots
            ):
                raise ContractError("audit surface roots must be unique and nonoverlapping")
            seen_roots.append(root_parts)
            candidate_root = require_contained_path(
                base.joinpath(*safe_relative_path(relative_root).parts),
                base,
                must_exist=False,
            )
            if not candidate_root.exists():
                state = "ABSENT"
                root_files: tuple[Path, ...] = ()
            else:
                reject_link(candidate_root)
                if not candidate_root.is_dir():
                    raise ContractError("audit surface root must be absent or a directory")
                files: list[Path] = []
                for child in candidate_root.rglob("*"):
                    reject_link(child)
                    if child.is_file():
                        if child.stat().st_nlink != 1:
                            raise ContractError(
                                "secret scan entries must be ordinary single-link files"
                            )
                        files.append(child)
                    elif not child.is_dir():
                        raise ContractError(
                            "audit surface root contains an unsupported entry"
                        )
                root_files = tuple(sorted(files))
                state = "NONEMPTY_DIRECTORY" if root_files else "EMPTY_DIRECTORY"
            for child in root_files:
                relative = child.relative_to(base).as_posix()
                if relative in discovered:
                    raise ContractError("audit surface roots discovered a duplicate file")
                discovered.add(relative)
            root_evidence.append(
                SurfaceRootEvidence(
                    surface=surface,
                    relative_path=relative_root,
                    state=state,
                    file_count=len(root_files),
                )
            )
        if tuple(sorted(discovered)) != relative_paths:
            raise ContractError(
                "declared surface file census differs from files below its roots"
            )
        counts.append((surface, len(relative_paths)))
        for relative in relative_paths:
            if relative in seen:
                raise ContractError("secret scan file appears in more than one surface")
            seen.add(relative)
            candidate = require_contained_path(base.joinpath(*safe_relative_path(relative).parts), base)
            if not candidate.is_file() or candidate.stat().st_nlink != 1:
                raise ContractError("secret scan entries must be ordinary single-link files")
            size = candidate.stat().st_size
            if size > maximum_file_bytes:
                raise ContractError("secret scan file exceeds the declared byte limit")

            categories: list[tuple[str, str | None, int | None]] = []
            lowered = candidate.name.lower()
            if lowered in _FORBIDDEN_SECRET_FILENAMES or lowered.endswith(".env"):
                digest = None
                categories.append(("FORBIDDEN_SECRET_FILENAME", None, None))
            else:
                payload = candidate.read_bytes()
                digest = sha256_bytes(payload)
                for category, pattern in _SECRET_PATTERNS:
                    match = pattern.search(payload)
                    if match is not None:
                        line_number = payload[: match.start()].count(b"\n") + 1
                        categories.append((category, digest, line_number))
            file_evidence.append(
                SurfaceFileEvidence(
                    surface=surface,
                    relative_path=relative,
                    byte_count=size,
                    file_sha256=digest,
                )
            )
            for category, digest, line_number in categories:
                unsigned = {
                    "surface": surface,
                    "relative_path": relative,
                    "category": category,
                    "line_number": line_number,
                    "byte_count": size,
                    "file_sha256": digest,
                }
                findings.append(
                    SecretFinding(
                        **unsigned,
                        finding_id=sha256_bytes(canonical_json_bytes(unsigned)),
                    )
                )

    findings.sort(key=lambda item: (item.surface, item.relative_path, item.category))
    unsigned_result = {
        "surface_counts": [
            {"surface": surface, "file_count": count} for surface, count in counts
        ],
        "surface_roots": [root.as_dict() for root in root_evidence],
        "files": [file.as_dict() for file in file_evidence],
        "findings": [finding.unsigned_dict() | {"finding_id": finding.finding_id} for finding in findings],
        "passed": not findings,
        "mechanics_only": True,
    }
    result = SecretScanResult(
        surface_counts=tuple(counts),
        surface_roots=tuple(root_evidence),
        files=tuple(file_evidence),
        findings=tuple(findings),
        passed=not findings,
        mechanics_only=True,
        scan_id=sha256_bytes(canonical_json_bytes(unsigned_result)),
    )
    result.validate()
    return result


@dataclass(frozen=True)
class TraceabilityRow:
    requirement_id: str
    threat_id: str
    severity: str
    priority: str
    authoritative_owner: str
    master_control: str
    gate: str
    enforcement: str
    test_evidence: str
    mutation_result: str
    evidence_id: str | None
    residual_risk: str
    disposition: str
    remediation_owner: str

    def validate(self) -> None:
        for field in ("requirement_id", "threat_id", "gate"):
            _identifier(getattr(self, field), f"traceability.{field}")
        for field in (
            "authoritative_owner",
            "master_control",
            "enforcement",
            "test_evidence",
            "residual_risk",
            "remediation_owner",
        ):
            _bounded_text(getattr(self, field), f"traceability.{field}")
        if self.severity not in SEVERITY_PRIORITIES:
            raise ContractError("traceability severity is invalid")
        if self.priority != SEVERITY_PRIORITIES[self.severity]:
            raise ContractError("traceability priority differs from severity")
        if self.mutation_result not in MUTATION_RESULTS:
            raise ContractError("traceability mutation result is invalid")
        if self.disposition not in TRACEABILITY_DISPOSITIONS:
            raise ContractError("traceability disposition is invalid")
        if self.evidence_id is not None:
            require_sha256(self.evidence_id, "traceability.evidence_id")
        if self.disposition == "CLOSED" and (
            self.mutation_result != "FAIL_CLOSED" or self.evidence_id is None
        ):
            raise ContractError("closed traceability rows require fail-closed evidence")
        if self.test_evidence == "MISSING_EVIDENCE" and self.disposition == "CLOSED":
            raise ContractError("missing test evidence cannot close a traceability row")


@dataclass(frozen=True)
class TraceabilityAssessment:
    classification: str
    requirement_count: int
    threat_count: int
    unresolved_critical_high: tuple[str, ...]
    assessment_id: str


def assess_traceability_matrix(
    requirement_ids: tuple[str, ...],
    threat_ids: tuple[str, ...],
    rows: tuple[TraceabilityRow, ...],
) -> TraceabilityAssessment:
    """Require exact requirement/threat closure and classify residual gaps."""

    requirements = _unique_identifiers(requirement_ids, "requirement_ids")
    threats = _unique_identifiers(threat_ids, "threat_ids")
    if type(rows) is not tuple or not rows:
        raise ContractError("traceability rows must be an exact nonempty tuple")
    for row in rows:
        if type(row) is not TraceabilityRow:
            raise ContractError("traceability matrix requires exact row values")
        row.validate()
    row_requirements = tuple(row.requirement_id for row in rows)
    if len(set(row_requirements)) != len(row_requirements):
        raise ContractError("traceability matrix has duplicate requirement rows")
    if set(row_requirements) != set(requirements):
        raise ContractError("traceability matrix does not exactly cover requirements")
    referenced_threats = {row.threat_id for row in rows}
    if referenced_threats != set(threats):
        raise ContractError("traceability matrix does not exactly cover threats")

    if any(row.mutation_result == "FALSE_PASS" for row in rows):
        classification = "BLOCKED"
    elif all(
        row.disposition == "CLOSED"
        and row.mutation_result == "FAIL_CLOSED"
        and row.evidence_id is not None
        for row in rows
    ):
        classification = "SUPPORTABLE"
    else:
        classification = "INSUFFICIENT_EVIDENCE"
    unresolved = tuple(
        row.requirement_id
        for row in rows
        if row.severity in {"Critical", "High"} and row.disposition != "CLOSED"
    )
    unsigned = {
        "classification": classification,
        "requirement_count": len(requirements),
        "threat_count": len(threats),
        "unresolved_critical_high": list(unresolved),
    }
    return TraceabilityAssessment(
        classification=classification,
        requirement_count=len(requirements),
        threat_count=len(threats),
        unresolved_critical_high=unresolved,
        assessment_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )


@dataclass(frozen=True)
class ProviderLineageEvidence:
    raw_bytes_sha256: str
    raw_byte_count: int
    response_headers_sha256: str
    request_contract_sha256: str
    request_lineage_sha256: str
    pagination_lineage_sha256: str
    page_sha256s: tuple[str, ...]
    page_byte_counts: tuple[int, ...]
    page_count: int
    page_census_id: str
    raw_landed_before_parse: bool
    evidence_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "raw_bytes_sha256": self.raw_bytes_sha256,
            "raw_byte_count": self.raw_byte_count,
            "response_headers_sha256": self.response_headers_sha256,
            "request_contract_sha256": self.request_contract_sha256,
            "request_lineage_sha256": self.request_lineage_sha256,
            "pagination_lineage_sha256": self.pagination_lineage_sha256,
            "page_sha256s": list(self.page_sha256s),
            "page_byte_counts": list(self.page_byte_counts),
            "page_count": self.page_count,
            "page_census_id": self.page_census_id,
            "raw_landed_before_parse": self.raw_landed_before_parse,
        }

    def validate(self) -> None:
        for field in (
            "raw_bytes_sha256",
            "response_headers_sha256",
            "request_contract_sha256",
            "request_lineage_sha256",
            "pagination_lineage_sha256",
        ):
            require_sha256(getattr(self, field), f"provider_lineage.{field}")
        if type(self.raw_byte_count) is not int or self.raw_byte_count < 1:
            raise ContractError("provider lineage raw byte count must be positive")
        if type(self.page_count) is not int or self.page_count < 1:
            raise ContractError("provider lineage page_count must be a positive integer")
        if (
            type(self.page_sha256s) is not tuple
            or len(self.page_sha256s) != self.page_count
        ):
            raise ContractError("provider lineage page hash census is incomplete")
        if (
            type(self.page_byte_counts) is not tuple
            or len(self.page_byte_counts) != self.page_count
            or any(type(value) is not int or value < 1 for value in self.page_byte_counts)
            or sum(self.page_byte_counts) != self.raw_byte_count
        ):
            raise ContractError("provider lineage page byte census is incomplete")
        for index, page_sha256 in enumerate(self.page_sha256s):
            require_sha256(page_sha256, f"provider_lineage.page_sha256s[{index}]")
        if self.raw_landed_before_parse is not True:
            raise ContractError("provider raw bytes must land before parse")
        require_sha256(self.page_census_id, "provider_lineage.page_census_id")
        page_census = {
            "raw_bytes_sha256": self.raw_bytes_sha256,
            "raw_byte_count": self.raw_byte_count,
            "page_sha256s": list(self.page_sha256s),
            "page_byte_counts": list(self.page_byte_counts),
            "page_count": self.page_count,
        }
        if self.page_census_id != sha256_bytes(canonical_json_bytes(page_census)):
            raise ContractError("provider lineage page census ID differs")
        require_sha256(self.evidence_id, "provider_lineage.evidence_id")
        if self.evidence_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("provider lineage evidence ID differs from its content")

    @classmethod
    def create(
        cls,
        *,
        raw_bytes: bytes,
        page_payloads: tuple[bytes, ...],
        expected_page_count: int,
        response_headers_sha256: str,
        request_contract_sha256: str,
        request_lineage_sha256: str,
        pagination_lineage_sha256: str,
        raw_landed_before_parse: bool,
    ) -> "ProviderLineageEvidence":
        if type(raw_bytes) is not bytes or not raw_bytes:
            raise ContractError("provider raw bytes must be nonempty exact bytes")
        if (
            type(page_payloads) is not tuple
            or not page_payloads
            or any(type(payload) is not bytes or not payload for payload in page_payloads)
        ):
            raise ContractError("provider pages must be a nonempty exact byte census")
        if type(expected_page_count) is not int or expected_page_count < 1:
            raise ContractError("provider expected page count must be positive")
        if len(page_payloads) != expected_page_count:
            raise ContractError("provider page census differs from the expected count")
        if raw_bytes != b"".join(page_payloads):
            raise ContractError(
                "provider raw bytes differ from the ordered page composition"
            )
        raw_bytes_sha256 = sha256_bytes(raw_bytes)
        page_sha256s = tuple(sha256_bytes(payload) for payload in page_payloads)
        page_byte_counts = tuple(len(payload) for payload in page_payloads)
        page_count = len(page_payloads)
        page_census = {
            "raw_bytes_sha256": raw_bytes_sha256,
            "raw_byte_count": len(raw_bytes),
            "page_sha256s": list(page_sha256s),
            "page_byte_counts": list(page_byte_counts),
            "page_count": page_count,
        }
        page_census_id = sha256_bytes(canonical_json_bytes(page_census))
        unsigned = {
            "raw_bytes_sha256": raw_bytes_sha256,
            "raw_byte_count": len(raw_bytes),
            "response_headers_sha256": response_headers_sha256,
            "request_contract_sha256": request_contract_sha256,
            "request_lineage_sha256": request_lineage_sha256,
            "pagination_lineage_sha256": pagination_lineage_sha256,
            "page_sha256s": list(page_sha256s),
            "page_byte_counts": list(page_byte_counts),
            "page_count": page_count,
            "page_census_id": page_census_id,
            "raw_landed_before_parse": raw_landed_before_parse,
        }
        result = cls(
            raw_bytes_sha256=raw_bytes_sha256,
            raw_byte_count=len(raw_bytes),
            response_headers_sha256=response_headers_sha256,
            request_contract_sha256=request_contract_sha256,
            request_lineage_sha256=request_lineage_sha256,
            pagination_lineage_sha256=pagination_lineage_sha256,
            page_sha256s=page_sha256s,
            page_byte_counts=page_byte_counts,
            page_count=page_count,
            page_census_id=page_census_id,
            raw_landed_before_parse=raw_landed_before_parse,
            evidence_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class ProspectiveControlProtocol:
    maximum_attempts: int
    attempts_used: int
    fixed_end_session: int
    current_session: int
    sealed_before_first_prediction: bool
    aggregate_blinded_until_fixed_end: bool
    missed_vintage_backfill_allowed: bool
    indirect_holdout_queries_allowed: bool
    failed_holdout_reuse_allowed: bool
    early_stop_policy_id: str
    expected_vintage_ids: tuple[str, ...]
    protocol_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "maximum_attempts": self.maximum_attempts,
            "attempts_used": self.attempts_used,
            "fixed_end_session": self.fixed_end_session,
            "current_session": self.current_session,
            "sealed_before_first_prediction": self.sealed_before_first_prediction,
            "aggregate_blinded_until_fixed_end": self.aggregate_blinded_until_fixed_end,
            "missed_vintage_backfill_allowed": self.missed_vintage_backfill_allowed,
            "indirect_holdout_queries_allowed": self.indirect_holdout_queries_allowed,
            "failed_holdout_reuse_allowed": self.failed_holdout_reuse_allowed,
            "early_stop_policy_id": self.early_stop_policy_id,
            "expected_vintage_ids": list(self.expected_vintage_ids),
        }

    def validate(self) -> None:
        if (
            type(self.maximum_attempts) is not int
            or self.maximum_attempts < 1
            or type(self.attempts_used) is not int
            or not 0 <= self.attempts_used <= self.maximum_attempts
        ):
            raise ContractError("prospective attempt budget is invalid")
        if (
            type(self.fixed_end_session) is not int
            or type(self.current_session) is not int
            or self.fixed_end_session < 1
            or self.current_session < 1
        ):
            raise ContractError("prospective fixed-end sessions are invalid")
        if self.sealed_before_first_prediction is not True:
            raise ContractError("prospective protocol was not sealed before prediction")
        if self.aggregate_blinded_until_fixed_end is not True:
            raise ContractError("prospective aggregate must remain blinded until fixed end")
        for field in (
            "missed_vintage_backfill_allowed",
            "indirect_holdout_queries_allowed",
            "failed_holdout_reuse_allowed",
        ):
            if getattr(self, field) is not False:
                raise ContractError(f"prospective prohibition differs: {field}")
        require_sha256(self.early_stop_policy_id, "prospective.early_stop_policy_id")
        expected = _unique_identifiers(
            self.expected_vintage_ids,
            "prospective.expected_vintage_ids",
        )
        if not expected:
            raise ContractError("prospective expected vintage census cannot be empty")
        require_sha256(self.protocol_id, "prospective.protocol_id")
        if self.protocol_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("prospective protocol ID differs from its content")

    @classmethod
    def create(cls, **values: object) -> "ProspectiveControlProtocol":
        unsigned = dict(values)
        result = cls(
            **unsigned,
            protocol_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        result.validate()
        return result


def require_next_attempt(protocol: ProspectiveControlProtocol) -> int:
    protocol.validate()
    if protocol.attempts_used >= protocol.maximum_attempts:
        raise ContractError("optional-stop attempt budget is exhausted")
    return protocol.attempts_used + 1


def require_direct_final_holdout_query(
    protocol: ProspectiveControlProtocol,
    *,
    query_kind: str,
    holdout_unlocked: bool,
) -> None:
    protocol.validate()
    if query_kind != "DIRECT_REGISTERED_FINAL_HOLDOUT":
        raise ContractError("indirect holdout queries are prohibited")
    if holdout_unlocked is not True:
        raise ContractError("final holdout query requires the exact authorized unlock")


def authorize_aggregate_read(protocol: ProspectiveControlProtocol) -> None:
    protocol.validate()
    if protocol.current_session < protocol.fixed_end_session:
        raise ContractError("prospective aggregate remains blinded until fixed end")


def verify_prospective_vintage_census(
    protocol: ProspectiveControlProtocol,
    *,
    observed_vintage_ids: tuple[str, ...],
    backfilled_vintage_ids: tuple[str, ...],
) -> str:
    protocol.validate()
    expected = protocol.expected_vintage_ids
    observed = _unique_identifiers(observed_vintage_ids, "observed_vintage_ids")
    backfilled = _unique_identifiers(backfilled_vintage_ids, "backfilled_vintage_ids")
    if observed != expected[: len(observed)]:
        raise ContractError("prospective census contains an undeclared or out-of-order vintage")
    if backfilled:
        raise ContractError("missed prospective vintages cannot be backfilled")
    if protocol.current_session < protocol.fixed_end_session:
        return "PROSPECTIVE_EVIDENCE_PENDING"
    if set(observed) != set(expected):
        return "INCONCLUSIVE_MISSED_VINTAGES"
    return "PASS_PROSPECTIVE_CENSUS_MECHANICS_ONLY"
