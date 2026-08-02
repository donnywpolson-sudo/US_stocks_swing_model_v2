from datetime import date, datetime, timezone

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.corporate_actions import BitemporalActionLedger, CorporateAction, CorporateActionCoverage
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.prospective_coverage import (
    COMPLETE,
    UNRESOLVED,
    CoverageRequirement,
    materialize_action_and_delisting_coverage,
)


AS_OF = datetime(2026, 8, 1, 20, tzinfo=timezone.utc)
RELEASE_ID = "a" * 64
SNAPSHOT_ID = "b" * 64
PROVIDER_ID = "c" * 64


def _ledger(*, covered: bool) -> BitemporalActionLedger:
    permit = SyntheticOnlyPermit.create(
        fixture_id=f"coverage-{covered}", scope="SYNTHETIC_CORPORATE_ACTION_LEDGER"
    )
    coverage = ()
    if covered:
        coverage = (
            CorporateActionCoverage.create(
                effective_start_session=date(2026, 7, 27),
                effective_end_session=date(2026, 8, 7),
                asset_scope="EXACT_ASSET_IDS",
                asset_ids=("asset-a",),
                received_at=AS_OF,
                source_snapshot_ids=(SNAPSHOT_ID,),
                provider_coverage_id=PROVIDER_ID,
                source_release_id=permit.permit_id,
                source_epoch="SYNTHETIC_ONLY",
            ),
        )
    return BitemporalActionLedger(
        synthetic_permit=permit,
        coverage=coverage,
    )


def test_coverage_census_preserves_unresolved_denominator_rows() -> None:
    census = materialize_action_and_delisting_coverage(
        _ledger(covered=True),
        (
            CoverageRequirement("asset-a", date(2026, 7, 27), date(2026, 8, 3)),
            CoverageRequirement("asset-b", date(2026, 7, 27), date(2026, 8, 3)),
        ),
        evidence_view_as_of=AS_OF,
    )
    assert len(census.assessments) == 2
    assert census.complete_count == 1
    assert census.unresolved_count == 1
    assert census.assessments[0].status == COMPLETE
    assert census.assessments[1].status == UNRESOLVED
    assert census.assessments[1].action_coverage_complete is False
    assert census.assessments[1].delisting_coverage_complete is False


def test_coverage_landed_after_cutoff_is_unresolved() -> None:
    census = materialize_action_and_delisting_coverage(
        _ledger(covered=True),
        (CoverageRequirement("asset-a", date(2026, 7, 27), date(2026, 8, 3)),),
        evidence_view_as_of=datetime(2026, 8, 1, 19, 59, tzinfo=timezone.utc),
    )
    assert census.assessments[0].status == UNRESOLVED
    assert census.unresolved_count == 1


def test_coverage_census_rejects_unsorted_or_duplicate_requirements() -> None:
    requirement = CoverageRequirement("asset-a", date(2026, 7, 27), date(2026, 8, 3))
    with pytest.raises(ContractError, match="sorted and unique"):
        materialize_action_and_delisting_coverage(
            _ledger(covered=True), (requirement, requirement), evidence_view_as_of=AS_OF
        )
