from pathlib import Path

from us_stocks_swing_model_v2.prospective_governance import (
    PREREGISTRATION_REQUIRED_FIELDS,
    build_historical_trial_census_workflow,
    build_prospective_preregistration_template,
)


REPO = Path(__file__).parents[1]


def test_historical_trial_census_workflow_never_substitutes_a_local_ledger() -> None:
    workflow = build_historical_trial_census_workflow()
    assert workflow["completion"]["exact_census_complete"] is False
    assert workflow["completion"]["status"] == "INDETERMINATE_BLOCKS_TRUSTED_GATE"
    assert workflow["conservative_counting"]["local_ledger_is_sufficient"] is False
    assert len(workflow["historical_trial_census_workflow_id"]) == 64


def test_preregistration_template_is_non_registering_while_s3_is_unconfigured() -> None:
    template = build_prospective_preregistration_template(repository_root=REPO)
    assert template["required_unselected_fields"] == list(PREREGISTRATION_REQUIRED_FIELDS)
    assert template["fixed_contract"]["sleeves"] == ["STOCK_LONG", "STOCK_SHORT", "ETF_LONG", "ETF_SHORT"]
    assert template["external_registry"]["status"] == "BACKEND_SELECTED_NOT_CONFIGURED"
    assert template["external_registry"]["registration_available"] is False
    assert all(value is False for value in template["authorities"].values())
