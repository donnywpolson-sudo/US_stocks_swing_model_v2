from datetime import date, datetime, timezone

from us_stocks_swing_model_v2.prospective_materializers import (
    ProspectiveCandidate, ProspectiveMaterializationContext, materialize_eligible_universe,
)
from us_stocks_swing_model_v2.prospective_release_plans import build_prospective_downstream_release_plan
from us_stocks_swing_model_v2.schemas import SecurityType


def _context() -> ProspectiveMaterializationContext:
    at = datetime(2026, 8, 3, 21, tzinfo=timezone.utc)
    return ProspectiveMaterializationContext("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, "prospective_sip_v1", date(2026, 8, 3), at, at, datetime(2026, 8, 4, tzinfo=timezone.utc))


def test_eligible_universe_release_plan_is_hash_bound_and_zero_write() -> None:
    context = _context()
    rows = materialize_eligible_universe(context, (ProspectiveCandidate("asset-a", "AAPL", SecurityType.STOCK, context.decision_at, True, True, True),))
    plan = build_prospective_downstream_release_plan(kind="eligible_universe", context=context, rows=rows)
    assert plan["publication"]["dataset"] == "eligible_universe"
    assert plan["publication"]["publication_authorized"] is False
    assert plan["authorities"]["release_write"] is False
    assert len(plan["downstream_release_plan_id"]) == 64


def test_feature_plan_retains_abstention_rows() -> None:
    context = _context()
    universe = materialize_eligible_universe(context, (ProspectiveCandidate("asset-a", "AAPL", SecurityType.STOCK, context.decision_at, False, True, True),))
    from us_stocks_swing_model_v2.prospective_materializers import FeatureMaterializationDecision
    features = (FeatureMaterializationDecision(universe[0], "ABSTAIN_UNRESOLVED_CAUSAL_LOOKBACK", universe[0].reason, None),)
    plan = build_prospective_downstream_release_plan(kind="features", context=context, rows=features, coverage_census_id="f" * 64)
    assert plan["payload"]["row_count"] == 1
    assert plan["payload"]["retained_abstention_rows"] == 1
