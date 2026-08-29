from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.corporate_actions import BitemporalActionLedger, CorporateActionCoverage
from us_stocks_swing_model_v2.effective_event_delisting_adapter import (
    EffectiveEventDelistingEvidenceDescriptor,
    build_effective_event_delisting_qualification_plan,
    require_configured_effective_event_delisting_adapter,
)
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.identity import (
    BitemporalIdentityLedger,
    IdentitySnapshot,
    IdentityVersion,
)
from us_stocks_swing_model_v2.outcomes import DailyBar, build_outcome
from us_stocks_swing_model_v2.prospective_coverage import CoverageRequirement, materialize_action_and_delisting_coverage
from us_stocks_swing_model_v2.prospective_materializers import (
    FEATURE_ABSTAIN,
    ProspectiveCandidate,
    ProspectiveMaterializationContext,
    materialize_eligible_universe,
    materialize_price_only_feature_rows,
)
from us_stocks_swing_model_v2.prospective_price_features import CausalPriceBar
from us_stocks_swing_model_v2.schemas import OutcomeStatus, SecurityType


REPO = Path(__file__).parents[1]
AT = datetime(2026, 8, 11, 21, tzinfo=timezone.utc)
IDS = {name: char * 64 for name, char in zip(("identity", "snapshot", "bars", "actions", "calendar"), "abcde")}


def _identity() -> tuple[BitemporalIdentityLedger, str]:
    permit = SyntheticOnlyPermit.create(
        fixture_id="waiting-period-identity",
        scope="SYNTHETIC_IDENTITY_LEDGER",
    )
    ledger = BitemporalIdentityLedger(synthetic_permit=permit)
    row = IdentityVersion(
        asset_id="aapl-id", symbol="AAPL", security_type=SecurityType.STOCK,
        listing_exchange="NASDAQ", active=True, eligible=True,
        membership_present=True, abstention_reason=None, effective_at=AT,
        known_at=AT, identity_snapshot_id="0" * 64,
        alpaca_snapshot_id="1" * 64, nasdaq_snapshot_id="2" * 64,
        nasdaq_file_created_at=AT,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
        synthetic_permit_ids=(permit.permit_id,),
    )
    provisional = IdentitySnapshot(
        snapshot_id="0" * 64, effective_at=AT, known_at=AT,
        complete_membership=True, alpaca_snapshot_id="1" * 64,
        nasdaq_snapshot_id="2" * 64, nasdaq_file_created_at=AT,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
        synthetic_permit_ids=(permit.permit_id,), rows=(row,),
    )
    unsigned = provisional.receipt_dict()
    unsigned.pop("snapshot_id")
    unsigned["rows"][0].pop("identity_snapshot_id")
    snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
    ledger.append_snapshot(replace(
        provisional,
        snapshot_id=snapshot_id,
        rows=(replace(row, identity_snapshot_id=snapshot_id),),
    ))
    return ledger, snapshot_id


def _ledger(*, covered: bool = True) -> BitemporalActionLedger:
    permit = SyntheticOnlyPermit.create(fixture_id="waiting-period", scope="SYNTHETIC_CORPORATE_ACTION_LEDGER")
    coverage = () if not covered else (CorporateActionCoverage.create(
        effective_start_session=date(2026, 7, 27), effective_end_session=date(2026, 8, 10),
        asset_scope="EXACT_ASSET_IDS", asset_ids=("aapl-id",), received_at=AT,
        source_snapshot_ids=("f" * 64,), provider_coverage_id="0" * 64,
        source_release_id=permit.permit_id, source_epoch="SYNTHETIC_ONLY",
    ),)
    return BitemporalActionLedger(synthetic_permit=permit, coverage=coverage)


def _context(
    ledger: BitemporalActionLedger,
    identity: BitemporalIdentityLedger,
    identity_snapshot_id: str,
) -> ProspectiveMaterializationContext:
    return ProspectiveMaterializationContext(
        identity_release_id=identity.release_id, identity_snapshot_id=identity_snapshot_id, bar_release_id=IDS["bars"],
        action_release_id=ledger.release_id, calendar_release_id=IDS["calendar"],
        identity_source_epoch=identity.source_epoch, bar_source_epoch="SYNTHETIC_BARS_ONLY",
        action_source_epoch=ledger.source_epoch,
        decision_session=date(2026, 8, 3), decision_at=AT, prediction_deadline_at=AT,
        information_barrier_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )


def test_unconfigured_adapter_rejects_alpaca_and_cannot_clear_coverage() -> None:
    plan = build_effective_event_delisting_qualification_plan(repository_root=REPO)
    assert plan["provider_selection_required"] is True
    assert plan["selected_backend"] is None
    assert plan["coverage_contract"]["semantics"] == "EFFECTIVE_EVENT_COMPLETENESS"
    assert plan["coverage_contract"]["scope"] == "EXACT_ASSET_IDS_AND_SESSION_INTERVAL"
    assert plan["failure_states"][0] == "BACKEND_UNSELECTED"
    assert "DELISTING_CENSUS_INCOMPLETE" in plan["failure_states"]
    assert plan["effective_event_coverage_usable"] is False
    assert plan["authorities"]["network_calls"] == 0
    assert all(value is False for key, value in plan["authorities"].items() if key != "network_calls")
    descriptor = EffectiveEventDelistingEvidenceDescriptor("alpaca", "alpaca_corporate_actions_v1", "a" * 64, ("b" * 64,), True, True, True)
    with pytest.raises(ContractError, match="Alpaca process-date"):
        require_configured_effective_event_delisting_adapter(descriptor, repository_root=REPO)


def test_synthetic_lineage_matures_only_with_complete_coverage_and_all_inputs() -> None:
    identity, identity_snapshot_id = _identity()
    ledger = _ledger()
    context = _context(ledger, identity, identity_snapshot_id)
    census = materialize_action_and_delisting_coverage(ledger, (CoverageRequirement("aapl-id", date(2026, 7, 27), date(2026, 8, 10)),), evidence_view_as_of=AT)
    universe = materialize_eligible_universe(context, (ProspectiveCandidate("aapl-id", "AAPL", SecurityType.STOCK, AT, True, True, True),))
    lookback = (date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31), date(2026, 8, 3))
    features = materialize_price_only_feature_rows(context, universe, sessions=lookback, bars_by_asset={"aapl-id": tuple(CausalPriceBar("aapl-id", item, 100.0, 101.0, AT) for item in lookback)}, identity=identity, coverage=census, actions=ledger)
    assert features[0].feature_row is not None
    calendar = SimpleNamespace(release_id=IDS["calendar"], trust_eligible=False, outcome_sessions=lambda session: (date(2026, 8, 4), date(2026, 8, 10)), interval=lambda start, end: (date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 10)))
    bars = {item: DailyBar("aapl-id", item, 100.0, 110.0, AT) for item in calendar.interval(date(2026, 8, 4), date(2026, 8, 10))}
    outcome = build_outcome(prediction_id="1" * 64, eligibility_census_id="2" * 64, asset_id="aapl-id", decision_session=date(2026, 8, 3), calendar=calendar, bars=bars, bar_release_id=IDS["bars"], actions=ledger, action_view_as_of=AT, source_epoch=ledger.source_epoch)
    assert outcome.status is OutcomeStatus.MATURED
    incomplete = _ledger(covered=False)
    incomplete_context = _context(incomplete, identity, identity_snapshot_id)
    incomplete_census = materialize_action_and_delisting_coverage(incomplete, (CoverageRequirement("aapl-id", date(2026, 7, 27), date(2026, 8, 10)),), evidence_view_as_of=AT)
    abstained = materialize_price_only_feature_rows(incomplete_context, universe, sessions=lookback, bars_by_asset={"aapl-id": tuple(CausalPriceBar("aapl-id", item, 100.0, 101.0, AT) for item in lookback)}, identity=identity, coverage=incomplete_census, actions=incomplete)
    assert abstained[0].status == FEATURE_ABSTAIN
