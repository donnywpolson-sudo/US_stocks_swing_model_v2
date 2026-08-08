"""Fail-closed prospective action and delisting coverage censuses.

This is an in-memory materializer only.  It deliberately does not acquire
provider data, publish releases, or convert an unresolved row into an absent
event.  The same governed effective-event coverage must cover both the
corporate-action and delisting questions for every requested asset/session
window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from .common import (
    canonical_json_bytes,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
)
from .corporate_actions import BitemporalActionLedger
from .errors import ContractError


COMPLETE = "COMPLETE_ACTION_AND_DELISTING_COVERAGE"
UNRESOLVED = "UNRESOLVED_ACTION_OR_DELISTING_COVERAGE"


@dataclass(frozen=True)
class CoverageRequirement:
    """One denominator row whose effective-event evidence must be complete."""

    asset_id: str
    start_session: date
    end_session: date

    def validate(self) -> None:
        if type(self.asset_id) is not str or not self.asset_id:
            raise ContractError("coverage requirement asset_id must be exact text")
        if (
            type(self.start_session) is not date
            or type(self.end_session) is not date
            or self.end_session < self.start_session
        ):
            raise ContractError("coverage requirement sessions are invalid")

    def as_dict(self) -> dict[str, str]:
        self.validate()
        return {
            "asset_id": self.asset_id,
            "start_session": self.start_session.isoformat(),
            "end_session": self.end_session.isoformat(),
        }


@dataclass(frozen=True)
class CoverageAssessment:
    asset_id: str
    start_session: date
    end_session: date
    action_coverage_complete: bool
    delisting_coverage_complete: bool
    status: str
    reason: str | None

    @property
    def complete(self) -> bool:
        return (
            self.action_coverage_complete
            and self.delisting_coverage_complete
            and self.status == COMPLETE
        )

    def validate(self) -> None:
        CoverageRequirement(
            self.asset_id,
            self.start_session,
            self.end_session,
        ).validate()
        if (
            type(self.action_coverage_complete) is not bool
            or type(self.delisting_coverage_complete) is not bool
        ):
            raise ContractError("coverage assessment flags must be boolean")
        covered = self.action_coverage_complete and self.delisting_coverage_complete
        if self.action_coverage_complete != self.delisting_coverage_complete:
            raise ContractError(
                "governed effective-event coverage must bind actions and delistings together"
            )
        if covered:
            if self.status != COMPLETE or self.reason is not None:
                raise ContractError("complete coverage assessment state differs")
        elif (
            self.status != UNRESOLVED
            or self.reason
            != "effective-event coverage is unavailable by the evidence cutoff"
        ):
            raise ContractError("unresolved coverage assessment state differs")

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "asset_id": self.asset_id,
            "start_session": self.start_session.isoformat(),
            "end_session": self.end_session.isoformat(),
            "action_coverage_complete": self.action_coverage_complete,
            "delisting_coverage_complete": self.delisting_coverage_complete,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProspectiveCoverageCensus:
    """Stable, denominator-preserving assessment for one evidence cutoff."""

    action_release_id: str
    source_epoch: str
    evidence_view_as_of: datetime
    trust_eligible: bool
    assessments: tuple[CoverageAssessment, ...]
    coverage_census_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "action_release_id": self.action_release_id,
            "source_epoch": self.source_epoch,
            "evidence_view_as_of": self.evidence_view_as_of.isoformat().replace("+00:00", "Z"),
            "trust_eligible": self.trust_eligible,
            "assessments": [item.as_dict() for item in self.assessments],
        }

    def validate(self) -> None:
        require_sha256(self.action_release_id, "coverage_census.action_release_id")
        if type(self.source_epoch) is not str or not self.source_epoch:
            raise ContractError("coverage census source_epoch is required")
        require_aware_utc(self.evidence_view_as_of, "coverage_census.evidence_view_as_of")
        if type(self.trust_eligible) is not bool:
            raise ContractError("coverage census trust state must be boolean")
        if type(self.assessments) is not tuple or not self.assessments:
            raise ContractError("coverage census assessments must be a nonempty tuple")
        identities: list[tuple[str, date, date]] = []
        for item in self.assessments:
            if type(item) is not CoverageAssessment:
                raise ContractError("coverage census contains an invalid assessment")
            item.validate()
            identities.append((item.asset_id, item.start_session, item.end_session))
        if identities != sorted(set(identities)):
            raise ContractError("coverage assessments must be sorted and unique")
        require_sha256(self.coverage_census_id, "coverage_census.coverage_census_id")
        if self.coverage_census_id != sha256_bytes(
            canonical_json_bytes(self.unsigned_dict())
        ):
            raise ContractError("coverage census ID differs from its content")

    @property
    def complete_count(self) -> int:
        return sum(item.complete for item in self.assessments)

    @property
    def unresolved_count(self) -> int:
        return len(self.assessments) - self.complete_count


def materialize_action_and_delisting_coverage(
    ledger: BitemporalActionLedger,
    requirements: Iterable[CoverageRequirement],
    *,
    evidence_view_as_of: datetime,
) -> ProspectiveCoverageCensus:
    """Assess every required interval without dropping unresolved rows.

    A governed effective-event coverage record certifies the provider's event
    census, including absence, for the requested asset/session interval.  It
    therefore answers both action and delisting completeness; an uncovered
    interval is explicitly unresolved for both, not treated as "no event".
    """

    if type(ledger) is not BitemporalActionLedger:
        raise ContractError("coverage materializer requires a corporate-action ledger")
    cutoff = require_aware_utc(evidence_view_as_of, "evidence_view_as_of")
    items = tuple(requirements)
    if not items:
        raise ContractError("coverage census cannot be empty")
    for item in items:
        if type(item) is not CoverageRequirement:
            raise ContractError("coverage census requires exact CoverageRequirement rows")
        item.validate()
    identities = [(item.asset_id, item.start_session, item.end_session) for item in items]
    if identities != sorted(set(identities)):
        raise ContractError("coverage requirements must be sorted and unique")

    assessments: list[CoverageAssessment] = []
    for item in items:
        covered = ledger.covers_effective_interval(
            item.asset_id, item.start_session, item.end_session, cutoff
        )
        assessments.append(
            CoverageAssessment(
                asset_id=item.asset_id,
                start_session=item.start_session,
                end_session=item.end_session,
                action_coverage_complete=covered,
                delisting_coverage_complete=covered,
                status=COMPLETE if covered else UNRESOLVED,
                reason=None if covered else "effective-event coverage is unavailable by the evidence cutoff",
            )
        )
    unsigned = {
        "action_release_id": ledger.release_id,
        "source_epoch": ledger.source_epoch,
        "evidence_view_as_of": cutoff.isoformat().replace("+00:00", "Z"),
        "trust_eligible": ledger.trust_eligible,
        "assessments": [item.as_dict() for item in assessments],
    }
    result = ProspectiveCoverageCensus(
        action_release_id=ledger.release_id,
        source_epoch=ledger.source_epoch,
        evidence_view_as_of=cutoff,
        trust_eligible=ledger.trust_eligible,
        assessments=tuple(assessments),
        coverage_census_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    result.validate()
    return result
