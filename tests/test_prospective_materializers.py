from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.corporate_actions import (
    ActionType,
    BitemporalActionLedger,
    CorporateAction,
    CorporateActionCoverage,
)
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.identity import (
    BitemporalIdentityLedger,
    IdentitySnapshot,
    IdentityVersion,
)
from us_stocks_swing_model_v2.prospective_coverage import (
    CoverageRequirement,
    materialize_action_and_delisting_coverage,
)
from us_stocks_swing_model_v2.prospective_materializers import (
    ABSTAIN,
    ELIGIBLE,
    FEATURE_ABSTAIN,
    FEATURE_SCHEMA_ID,
    ProspectiveCandidate,
    ProspectiveMaterializationContext,
    eligible_universe_census_id,
    materialize_eligible_universe,
    materialize_price_only_feature_rows,
)
from us_stocks_swing_model_v2.prospective_price_features import (
    CausalPriceBar,
    READY_STATUS,
)
from us_stocks_swing_model_v2.schemas import SecurityType


AT = datetime(2026, 8, 3, 21, tzinfo=timezone.utc)
IDS = {
    name: char * 64
    for name, char in zip(("bars", "calendar"), "cd")
}
SESSIONS = tuple(date(2026, 7, day) for day in (27, 28, 29, 30, 31)) + (
    date(2026, 8, 3),
)


def _identity() -> tuple[BitemporalIdentityLedger, str]:
    permit = SyntheticOnlyPermit.create(
        fixture_id="materializer-identity",
        scope="SYNTHETIC_IDENTITY_LEDGER",
    )
    ledger = BitemporalIdentityLedger(synthetic_permit=permit)
    alpaca_snapshot_id = "1" * 64
    nasdaq_snapshot_id = "2" * 64
    rows = (
        IdentityVersion(
            asset_id="aapl-id",
            symbol="AAPL",
            security_type=SecurityType.STOCK,
            listing_exchange="NASDAQ",
            active=True,
            eligible=True,
            membership_present=True,
            abstention_reason=None,
            effective_at=AT,
            known_at=AT,
            identity_snapshot_id="0" * 64,
            alpaca_snapshot_id=alpaca_snapshot_id,
            nasdaq_snapshot_id=nasdaq_snapshot_id,
            nasdaq_file_created_at=AT,
            evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
            synthetic_permit_ids=(permit.permit_id,),
        ),
        IdentityVersion(
            asset_id="spy-id",
            symbol="SPY",
            security_type=SecurityType.ETF,
            listing_exchange="NYSEARCA",
            active=False,
            eligible=False,
            membership_present=False,
            abstention_reason="absent_from_complete_snapshot",
            effective_at=AT,
            known_at=AT,
            identity_snapshot_id="0" * 64,
            alpaca_snapshot_id=alpaca_snapshot_id,
            nasdaq_snapshot_id=None,
            nasdaq_file_created_at=None,
            evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
            synthetic_permit_ids=(permit.permit_id,),
        ),
    )
    provisional = IdentitySnapshot(
        snapshot_id="0" * 64,
        effective_at=AT,
        known_at=AT,
        complete_membership=True,
        alpaca_snapshot_id=alpaca_snapshot_id,
        nasdaq_snapshot_id=nasdaq_snapshot_id,
        nasdaq_file_created_at=AT,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
        synthetic_permit_ids=(permit.permit_id,),
        rows=rows,
    )
    unsigned = provisional.receipt_dict()
    unsigned.pop("snapshot_id")
    for row in unsigned["rows"]:
        row.pop("identity_snapshot_id")
    snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
    snapshot = replace(
        provisional,
        snapshot_id=snapshot_id,
        rows=tuple(replace(row, identity_snapshot_id=snapshot_id) for row in rows),
    )
    ledger.append_snapshot(snapshot)
    return ledger, snapshot_id


def _actions(*, with_event: bool = False) -> tuple[BitemporalActionLedger, object]:
    permit = SyntheticOnlyPermit.create(
        fixture_id="materializer-actions",
        scope="SYNTHETIC_CORPORATE_ACTION_LEDGER",
    )
    action_rows = (
        CorporateAction(
            action_id="aapl-delisting-event",
            asset_id="aapl-id",
            action_type=ActionType.DELISTING,
            effective_session=date(2026, 7, 30),
            announced_at=None,
            received_at=AT,
            revision=1,
            source_snapshot_id="f" * 64,
            source_release_id=permit.permit_id,
            source_epoch="SYNTHETIC_ONLY",
            raw_row_sha256="e" * 64,
        ),
    ) if with_event else ()
    ledger = BitemporalActionLedger(
        synthetic_permit=permit,
        actions=action_rows,
        coverage=(
            CorporateActionCoverage.create(
                effective_start_session=SESSIONS[0],
                effective_end_session=SESSIONS[-1],
                asset_scope="EXACT_ASSET_IDS",
                asset_ids=("aapl-id",),
                received_at=AT,
                source_snapshot_ids=("f" * 64,),
                provider_coverage_id="0" * 64,
                source_release_id=permit.permit_id,
                source_epoch="SYNTHETIC_ONLY",
            ),
        ),
    )
    census = materialize_action_and_delisting_coverage(
        ledger,
        (CoverageRequirement("aapl-id", SESSIONS[0], SESSIONS[-1]),),
        evidence_view_as_of=AT,
    )
    return ledger, census


def _context(
    identity: BitemporalIdentityLedger,
    identity_snapshot_id: str,
    actions: BitemporalActionLedger,
) -> ProspectiveMaterializationContext:
    return ProspectiveMaterializationContext(
        identity_release_id=identity.release_id,
        identity_snapshot_id=identity_snapshot_id,
        bar_release_id=IDS["bars"],
        action_release_id=actions.release_id,
        calendar_release_id=IDS["calendar"],
        identity_source_epoch=identity.source_epoch,
        bar_source_epoch="SYNTHETIC_BARS_ONLY",
        action_source_epoch=actions.source_epoch,
        decision_session=SESSIONS[-1],
        decision_at=AT,
        prediction_deadline_at=AT,
        information_barrier_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )


def _bars() -> dict[str, tuple[CausalPriceBar, ...]]:
    return {
        "aapl-id": tuple(
            CausalPriceBar("aapl-id", session, 100 + index, 101 + index, AT)
            for index, session in enumerate(SESSIONS)
        )
    }


def _universe(context: ProspectiveMaterializationContext):
    return materialize_eligible_universe(
        context,
        (
            ProspectiveCandidate(
                "aapl-id", "AAPL", SecurityType.STOCK, AT, True, True, True
            ),
            ProspectiveCandidate(
                "spy-id", "SPY", SecurityType.ETF, AT, False, True, False
            ),
        ),
    )


def test_universe_and_features_keep_incomplete_candidates_as_abstentions() -> None:
    identity, snapshot_id = _identity()
    actions, coverage = _actions()
    context = _context(identity, snapshot_id, actions)
    universe = _universe(context)
    assert [item.status for item in universe] == [ELIGIBLE, ABSTAIN]
    assert universe[0].sleeve_evidence == ("STOCK_LONG", "STOCK_SHORT")
    assert universe[1].sleeve_evidence == ()

    features = materialize_price_only_feature_rows(
        context,
        universe,
        sessions=SESSIONS,
        bars_by_asset=_bars(),
        identity=identity,
        coverage=coverage,
        actions=actions,
    )
    assert features[0].status == READY_STATUS
    assert features[0].feature_row is not None
    assert features[0].feature_row.feature_schema_id == FEATURE_SCHEMA_ID
    assert features[0].feature_row.source_epoch == context.bar_source_epoch
    assert features[0].feature_row.point_in_time_state == "PIT_UNRESOLVED"
    assert features[1].status == FEATURE_ABSTAIN
    assert features[1].feature_row is None


def test_governed_event_census_never_creates_a_feature_row() -> None:
    identity, snapshot_id = _identity()
    actions, coverage = _actions(with_event=True)
    context = _context(identity, snapshot_id, actions)
    features = materialize_price_only_feature_rows(
        context,
        _universe(context),
        sessions=SESSIONS,
        bars_by_asset=_bars(),
        identity=identity,
        coverage=coverage,
        actions=actions,
    )
    assert features[0].status == FEATURE_ABSTAIN
    assert features[0].feature_row is None
    assert "action or delisting event" in str(features[0].reason)


def test_forged_or_noncanonical_universe_census_is_rejected() -> None:
    identity, snapshot_id = _identity()
    actions, coverage = _actions()
    context = _context(identity, snapshot_id, actions)
    universe = _universe(context)
    forged = replace(
        universe[0],
        candidate=replace(universe[0].candidate, membership_active=False),
    )
    with pytest.raises(ContractError, match="differs from governed identity and bar"):
        materialize_price_only_feature_rows(
            context,
            (forged, universe[1]),
            sessions=SESSIONS,
            bars_by_asset=_bars(),
            identity=identity,
            coverage=coverage,
            actions=actions,
        )
    with pytest.raises(ContractError, match="sorted and unique"):
        eligible_universe_census_id(context, tuple(reversed(universe)))


def test_coverage_window_and_evidence_view_are_bound_to_context() -> None:
    identity, snapshot_id = _identity()
    actions, _ = _actions()
    context = _context(identity, snapshot_id, actions)
    universe = _universe(context)
    late = materialize_action_and_delisting_coverage(
        actions,
        (CoverageRequirement("aapl-id", SESSIONS[0], SESSIONS[-1]),),
        evidence_view_as_of=AT + timedelta(minutes=1),
    )
    with pytest.raises(ContractError, match="evidence view differs"):
        materialize_price_only_feature_rows(
            context,
            universe,
            sessions=SESSIONS,
            bars_by_asset=_bars(),
            identity=identity,
            coverage=late,
            actions=actions,
        )

    narrow = materialize_action_and_delisting_coverage(
        actions,
        (CoverageRequirement("aapl-id", SESSIONS[1], SESSIONS[-1]),),
        evidence_view_as_of=AT,
    )
    with pytest.raises(ContractError, match="does not contain"):
        materialize_price_only_feature_rows(
            context,
            universe,
            sessions=SESSIONS,
            bars_by_asset=_bars(),
            identity=identity,
            coverage=narrow,
            actions=actions,
        )


def test_multi_source_epochs_are_bound_independently() -> None:
    identity, snapshot_id = _identity()
    actions, coverage = _actions()
    context = _context(identity, snapshot_id, actions)
    universe = _universe(context)

    assert context.bar_source_epoch not in {
        context.identity_source_epoch,
        context.action_source_epoch,
    }
    baseline_census_id = eligible_universe_census_id(context, universe)
    for field in (
        "identity_source_epoch",
        "bar_source_epoch",
        "action_source_epoch",
    ):
        changed = replace(context, **{field: f"CHANGED_{field.upper()}"})
        assert eligible_universe_census_id(changed, universe) != baseline_census_id

    with pytest.raises(ContractError, match="identity ledger provenance differs"):
        materialize_price_only_feature_rows(
            replace(context, identity_source_epoch="WRONG_IDENTITY_EPOCH"),
            universe,
            sessions=SESSIONS,
            bars_by_asset=_bars(),
            identity=identity,
            coverage=coverage,
            actions=actions,
        )
    with pytest.raises(ContractError, match="coverage census provenance differs"):
        materialize_price_only_feature_rows(
            replace(context, action_source_epoch="WRONG_ACTION_EPOCH"),
            universe,
            sessions=SESSIONS,
            bars_by_asset=_bars(),
            identity=identity,
            coverage=coverage,
            actions=actions,
        )


@pytest.mark.parametrize(
    "field",
    ("identity_source_epoch", "bar_source_epoch", "action_source_epoch"),
)
def test_context_requires_each_source_epoch(field: str) -> None:
    identity, snapshot_id = _identity()
    actions, _ = _actions()
    with pytest.raises(ContractError, match=field):
        replace(_context(identity, snapshot_id, actions), **{field: ""}).validate()
