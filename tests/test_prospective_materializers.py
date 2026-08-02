from datetime import date, datetime, timezone

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.corporate_actions import BitemporalActionLedger, CorporateActionCoverage
from us_stocks_swing_model_v2.prospective_coverage import CoverageRequirement, materialize_action_and_delisting_coverage
from us_stocks_swing_model_v2.prospective_materializers import (
    ABSTAIN, ELIGIBLE, FEATURE_ABSTAIN, FEATURE_SCHEMA_ID,
    ProspectiveCandidate, ProspectiveMaterializationContext,
    materialize_eligible_universe, materialize_price_only_feature_rows,
)
from us_stocks_swing_model_v2.prospective_price_features import CausalPriceBar, READY_STATUS
from us_stocks_swing_model_v2.schemas import SecurityType


AT = datetime(2026, 8, 3, 21, tzinfo=timezone.utc)
IDS = {name: char * 64 for name, char in zip(("identity", "snapshot", "bars", "actions", "calendar"), "abcde")}


def _context(*, action_release_id: str = IDS["actions"], source_epoch: str = "prospective_sip_v1") -> ProspectiveMaterializationContext:
    return ProspectiveMaterializationContext(
        identity_release_id=IDS["identity"], identity_snapshot_id=IDS["snapshot"],
        bar_release_id=IDS["bars"], action_release_id=action_release_id,
        calendar_release_id=IDS["calendar"], source_epoch=source_epoch,
        decision_session=date(2026, 8, 3), decision_at=AT,
        prediction_deadline_at=AT, information_barrier_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )


def _coverage():
    permit = SyntheticOnlyPermit.create(fixture_id="materializer", scope="SYNTHETIC_CORPORATE_ACTION_LEDGER")
    ledger = BitemporalActionLedger(
        synthetic_permit=permit,
        coverage=(CorporateActionCoverage.create(
            effective_start_session=date(2026, 7, 27), effective_end_session=date(2026, 8, 7),
            asset_scope="EXACT_ASSET_IDS", asset_ids=("aapl-id",), received_at=AT,
            source_snapshot_ids=("f" * 64,), provider_coverage_id="0" * 64,
            source_release_id=permit.permit_id, source_epoch="SYNTHETIC_ONLY",
        ),),
    )
    census = materialize_action_and_delisting_coverage(
        ledger, (CoverageRequirement("aapl-id", date(2026, 7, 27), date(2026, 8, 3)),), evidence_view_as_of=AT
    )
    return census


def test_universe_and_features_keep_incomplete_candidates_as_abstentions() -> None:
    coverage = _coverage()
    context = _context(action_release_id=coverage.action_release_id, source_epoch=coverage.source_epoch)
    candidates = (
        ProspectiveCandidate("aapl-id", "AAPL", SecurityType.STOCK, AT, True, True, True),
        ProspectiveCandidate("spy-id", "SPY", SecurityType.ETF, AT, False, True, True),
    )
    universe = materialize_eligible_universe(context, candidates)
    assert [item.status for item in universe] == [ELIGIBLE, ABSTAIN]
    assert universe[0].sleeve_evidence == ("STOCK_LONG", "STOCK_SHORT")
    assert universe[1].sleeve_evidence == ()
    sessions = tuple(date(2026, 7, day) for day in (27, 28, 29, 30, 31)) + (date(2026, 8, 3),)
    bars = tuple(CausalPriceBar("aapl-id", session, 100 + i, 101 + i, AT) for i, session in enumerate(sessions))
    features = materialize_price_only_feature_rows(
        context, universe, sessions=sessions, bars_by_asset={"aapl-id": bars}, coverage=coverage, action_or_delisting_sessions={}
    )
    assert features[0].status == READY_STATUS
    assert features[0].feature_row is not None
    assert features[0].feature_row.feature_schema_id == FEATURE_SCHEMA_ID
    assert features[1].status == FEATURE_ABSTAIN
    assert features[1].feature_row is None


def test_missing_coverage_or_event_never_creates_a_feature_row() -> None:
    coverage = _coverage()
    context = _context(action_release_id=coverage.action_release_id, source_epoch=coverage.source_epoch)
    universe = materialize_eligible_universe(context, (ProspectiveCandidate("aapl-id", "AAPL", SecurityType.STOCK, AT, True, True, True),))
    sessions = tuple(date(2026, 7, day) for day in (27, 28, 29, 30, 31)) + (date(2026, 8, 3),)
    bars = tuple(CausalPriceBar("aapl-id", session, 100, 101, AT) for session in sessions)
    missing = materialize_price_only_feature_rows(context, universe, sessions=sessions, bars_by_asset={"aapl-id": bars}, coverage=coverage, action_or_delisting_sessions={"aapl-id": frozenset({date(2026, 7, 30)})})
    assert missing[0].status == FEATURE_ABSTAIN
    assert missing[0].feature_row is None
