"""In-memory, fail-closed prospective eligible-universe and feature builders.

These builders are deliberately not release publishers.  They make the exact
row-level decisions a later plan-first publisher must serialize, while keeping
unresolved candidates in the output census and keeping outcome construction in
``outcomes.build_outcome`` (which requires the actual D1--D5 evidence window).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping

from .common import canonical_json_bytes, require_aware_utc, require_sha256, sha256_bytes
from .errors import ContractError
from .prospective_coverage import ProspectiveCoverageCensus
from .prospective_price_features import (
    CausalPriceBar,
    READY_STATUS,
    materialize_price_only_features,
)
from .schemas import FeatureRow, SecurityType


ELIGIBLE = "ELIGIBLE_PROSPECTIVE_PIT"
ABSTAIN = "ABSTAIN_UNRESOLVED_ELIGIBILITY"
FEATURE_ABSTAIN = "ABSTAIN_UNRESOLVED_CAUSAL_LOOKBACK"
FEATURE_SCHEMA_ID = "prospective_price_only_v1"
SLEEVES = ("STOCK_LONG", "STOCK_SHORT", "ETF_LONG", "ETF_SHORT")


@dataclass(frozen=True)
class ProspectiveMaterializationContext:
    identity_release_id: str
    identity_snapshot_id: str
    bar_release_id: str
    action_release_id: str
    calendar_release_id: str
    source_epoch: str
    decision_session: date
    decision_at: datetime
    prediction_deadline_at: datetime
    information_barrier_at: datetime

    def validate(self) -> None:
        for name in (
            "identity_release_id", "identity_snapshot_id", "bar_release_id",
            "action_release_id", "calendar_release_id",
        ):
            require_sha256(getattr(self, name), f"prospective_context.{name}")
        if type(self.source_epoch) is not str or not self.source_epoch:
            raise ContractError("prospective context source_epoch is required")
        if type(self.decision_session) is not date:
            raise ContractError("prospective context decision_session must be a date")
        decision = require_aware_utc(self.decision_at, "decision_at")
        deadline = require_aware_utc(self.prediction_deadline_at, "prediction_deadline_at")
        barrier = require_aware_utc(self.information_barrier_at, "information_barrier_at")
        if deadline < decision or barrier <= deadline:
            raise ContractError("prospective context decision chronology is invalid")


@dataclass(frozen=True)
class ProspectiveCandidate:
    asset_id: str
    symbol: str
    security_type: SecurityType
    identity_known_at: datetime
    membership_active: bool
    asset_type_confirmed: bool
    bar_present: bool

    def validate(self) -> None:
        if type(self.asset_id) is not str or not self.asset_id:
            raise ContractError("candidate asset_id must be exact text")
        if type(self.symbol) is not str or not self.symbol or self.symbol != self.symbol.upper():
            raise ContractError("candidate symbol must be canonical uppercase text")
        if self.security_type not in {SecurityType.STOCK, SecurityType.ETF, SecurityType.UNKNOWN}:
            raise ContractError("candidate security type is invalid")
        require_aware_utc(self.identity_known_at, "identity_known_at")
        if any(type(value) is not bool for value in (
            self.membership_active, self.asset_type_confirmed, self.bar_present
        )):
            raise ContractError("candidate evidence flags must be boolean")


@dataclass(frozen=True)
class EligibleUniverseDecision:
    candidate: ProspectiveCandidate
    status: str
    reason: str | None
    sleeve_evidence: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return self.status == ELIGIBLE

    def as_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.candidate.asset_id,
            "symbol": self.candidate.symbol,
            "security_type": self.candidate.security_type.value,
            "status": self.status,
            "reason": self.reason,
            "sleeve_evidence": list(self.sleeve_evidence),
        }


@dataclass(frozen=True)
class FeatureMaterializationDecision:
    universe: EligibleUniverseDecision
    status: str
    reason: str | None
    feature_row: FeatureRow | None


def materialize_eligible_universe(
    context: ProspectiveMaterializationContext,
    candidates: Iterable[ProspectiveCandidate],
) -> tuple[EligibleUniverseDecision, ...]:
    """Create one retained decision per candidate without membership carry-forward."""

    context.validate()
    items = tuple(candidates)
    if not items:
        raise ContractError("eligible-universe census cannot be empty")
    for candidate in items:
        if type(candidate) is not ProspectiveCandidate:
            raise ContractError("eligible-universe requires exact ProspectiveCandidate rows")
        candidate.validate()
    identities = [(item.asset_id, item.symbol) for item in items]
    if identities != sorted(set(identities)):
        raise ContractError("eligible-universe candidates must be sorted and unique")

    decisions: list[EligibleUniverseDecision] = []
    for candidate in items:
        if candidate.identity_known_at > context.decision_at:
            reason = "identity evidence was unavailable by the decision time"
        elif not candidate.membership_active:
            reason = "membership is unresolved or inactive for the decision session"
        elif not candidate.asset_type_confirmed or candidate.security_type is SecurityType.UNKNOWN:
            reason = "stock or ETF asset type is unresolved"
        elif not candidate.bar_present:
            reason = "canonical SIP bar evidence is missing"
        else:
            sleeves = (
                ("STOCK_LONG", "STOCK_SHORT")
                if candidate.security_type is SecurityType.STOCK
                else ("ETF_LONG", "ETF_SHORT")
            )
            decisions.append(EligibleUniverseDecision(candidate, ELIGIBLE, None, sleeves))
            continue
        decisions.append(EligibleUniverseDecision(candidate, ABSTAIN, reason, ()))
    return tuple(decisions)


def eligible_universe_census_id(
    context: ProspectiveMaterializationContext,
    decisions: Iterable[EligibleUniverseDecision],
) -> str:
    """Deterministic binding for a later non-active release plan, not a release ID."""

    context.validate()
    items = tuple(decisions)
    if not items:
        raise ContractError("eligible-universe decisions cannot be empty")
    if any(type(item) is not EligibleUniverseDecision for item in items):
        raise ContractError("eligible-universe census contains an invalid decision")
    return sha256_bytes(canonical_json_bytes({
        "context": {
            "identity_release_id": context.identity_release_id,
            "identity_snapshot_id": context.identity_snapshot_id,
            "bar_release_id": context.bar_release_id,
            "action_release_id": context.action_release_id,
            "calendar_release_id": context.calendar_release_id,
            "source_epoch": context.source_epoch,
            "decision_session": context.decision_session.isoformat(),
        },
        "decisions": [item.as_dict() for item in items],
    }))


def materialize_price_only_feature_rows(
    context: ProspectiveMaterializationContext,
    universe: Iterable[EligibleUniverseDecision],
    *,
    sessions: tuple[date, ...],
    bars_by_asset: Mapping[str, Iterable[CausalPriceBar]],
    coverage: ProspectiveCoverageCensus,
    action_or_delisting_sessions: Mapping[str, frozenset[date]],
) -> tuple[FeatureMaterializationDecision, ...]:
    """Emit frozen feature rows only for fully causal, eligible candidates.

    Non-ready candidates remain returned as explicit abstentions.  The function
    cannot read outcomes, labels, cross-sectional inputs, or release payloads.
    """

    context.validate()
    if coverage.action_release_id != context.action_release_id or coverage.source_epoch != context.source_epoch:
        raise ContractError("coverage census provenance differs from materialization context")
    coverage_by_asset = {item.asset_id: item for item in coverage.assessments}
    entries = tuple(universe)
    if not entries:
        raise ContractError("feature materialization requires an eligible-universe census")
    results: list[FeatureMaterializationDecision] = []
    for entry in entries:
        if type(entry) is not EligibleUniverseDecision:
            raise ContractError("feature materialization contains an invalid universe decision")
        candidate = entry.candidate
        if not entry.eligible:
            results.append(FeatureMaterializationDecision(entry, FEATURE_ABSTAIN, entry.reason, None))
            continue
        coverage_row = coverage_by_asset.get(candidate.asset_id)
        if coverage_row is None:
            results.append(FeatureMaterializationDecision(entry, FEATURE_ABSTAIN, "action/delisting coverage denominator row is missing", None))
            continue
        bars = tuple(bars_by_asset.get(candidate.asset_id, ()))
        if not bars:
            results.append(FeatureMaterializationDecision(entry, FEATURE_ABSTAIN, "missing required causal OHLC evidence", None))
            continue
        feature_result = materialize_price_only_features(
            bars,
            sessions=sessions,
            decision_session=context.decision_session,
            decision_at=context.decision_at,
            action_coverage_complete=coverage_row.complete,
            action_or_delisting_sessions=action_or_delisting_sessions.get(candidate.asset_id, frozenset()),
        )
        if len(feature_result) != 1 or feature_result[0].asset_id != candidate.asset_id:
            raise ContractError("feature bars must contain exactly one candidate asset")
        result = feature_result[0]
        if result.status != READY_STATUS or result.values is None or result.feature_available_at is None:
            results.append(FeatureMaterializationDecision(entry, FEATURE_ABSTAIN, result.reason, None))
            continue
        row = FeatureRow(
            asset_id=candidate.asset_id,
            symbol=candidate.symbol,
            security_type=candidate.security_type,
            decision_session=context.decision_session,
            decision_at=context.decision_at,
            available_at=result.feature_available_at,
            source_release_id=context.bar_release_id,
            feature_schema_id=FEATURE_SCHEMA_ID,
            identity_release_id=context.identity_release_id,
            security_type_evidence_id=context.identity_snapshot_id,
            calendar_release_id=context.calendar_release_id,
            action_release_id=context.action_release_id,
            source_epoch=context.source_epoch,
            identity_known_at=candidate.identity_known_at,
            point_in_time_state="PIT_CONFIRMED",
            prediction_deadline_at=context.prediction_deadline_at,
            information_barrier_at=context.information_barrier_at,
            values=result.values,
        )
        row.validate()
        results.append(FeatureMaterializationDecision(entry, READY_STATUS, None, row))
    return tuple(results)
