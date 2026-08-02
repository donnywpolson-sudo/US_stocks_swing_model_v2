"""Plan-only publishers for trusted prospective downstream releases.

The functions serialize no files and never call ``AtomicReleasePublisher``.
They freeze the exact materialized census, lineage, and payload hashes which a
later separately authorized publisher must reproduce.
"""

from __future__ import annotations

from typing import Any, Iterable

from .common import canonical_json_bytes, sha256_bytes
from .errors import ContractError
from .prospective_materializers import (
    FeatureMaterializationDecision,
    EligibleUniverseDecision,
    ProspectiveMaterializationContext,
)
from .schemas import OutcomeRow


_CONTRACTS = {
    "eligible_universe": {"dataset": "eligible_universe", "role": "derived_causal", "payload": "eligible_universe.json"},
    "features": {"dataset": "features", "role": "feature_only", "payload": "feature_materialization.json"},
    "outcomes": {"dataset": "outcomes", "role": "outcome_only", "payload": "outcomes.json"},
}


def _row_payload(kind: str, rows: Iterable[object]) -> list[dict[str, object]]:
    values = tuple(rows)
    if not values:
        raise ContractError("prospective release plan requires a nonempty retained census")
    if kind == "eligible_universe":
        if any(type(item) is not EligibleUniverseDecision for item in values):
            raise ContractError("eligible-universe release plan rows are invalid")
        return [item.as_dict() for item in values]  # type: ignore[union-attr]
    if kind == "features":
        if any(type(item) is not FeatureMaterializationDecision for item in values):
            raise ContractError("feature release plan rows are invalid")
        return [{
            "universe": item.universe.as_dict(),
            "status": item.status,
            "reason": item.reason,
            "feature_row": item.feature_row.receipt_dict() if item.feature_row else None,
        } for item in values]  # type: ignore[union-attr]
    if kind == "outcomes":
        if any(type(item) is not OutcomeRow for item in values):
            raise ContractError("outcome release plan rows are invalid")
        return [item.as_dict() for item in values]  # type: ignore[union-attr]
    raise ContractError("prospective release kind is invalid")


def build_prospective_downstream_release_plan(
    *,
    kind: str,
    context: ProspectiveMaterializationContext,
    rows: Iterable[object],
    coverage_census_id: str | None = None,
) -> dict[str, object]:
    """Return an exact no-write publication plan for one downstream dataset."""
    context.validate()
    if kind not in _CONTRACTS:
        raise ContractError("prospective release kind is invalid")
    payload_rows = _row_payload(kind, rows)
    if coverage_census_id is not None and (type(coverage_census_id) is not str or len(coverage_census_id) != 64):
        raise ContractError("coverage census ID is invalid")
    contract = _CONTRACTS[kind]
    payload = {"schema_version": 1, "rows": payload_rows}
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "mode": "PROSPECTIVE_DOWNSTREAM_RELEASE_PLAN_ONLY",
        "kind": kind,
        "publication": {**contract, "quality_state": "PASS", "publication_authorized": False},
        "lineage": {
            "identity_release_id": context.identity_release_id,
            "identity_snapshot_id": context.identity_snapshot_id,
            "bar_release_id": context.bar_release_id,
            "action_release_id": context.action_release_id,
            "calendar_release_id": context.calendar_release_id,
            "source_epoch": context.source_epoch,
            "decision_session": context.decision_session.isoformat(),
            "coverage_census_id": coverage_census_id,
        },
        "payload": {"sha256": sha256_bytes(canonical_json_bytes(payload)), "row_count": len(payload_rows), "retained_abstention_rows": sum(1 for row in payload_rows if row.get("status", "") != "ELIGIBLE_PROSPECTIVE_PIT" and row.get("status", "") != "READY_CAUSAL_PRICE_ONLY_V1" and kind != "outcomes")},
        "authorities": {"release_write": False, "source_activation": False, "outcome_access": False, "training": False, "evaluation": False},
    }
    return {**unsigned, "downstream_release_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
