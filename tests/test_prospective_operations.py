from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.prospective_governance import HISTORICAL_CENSUS_SOURCES
from us_stocks_swing_model_v2.prospective_operations import (
    HistoricalTrialCensusIntake, build_historical_trial_census_intake_plan,
    build_s3_object_lock_provisioning_checklist,
)
from us_stocks_swing_model_v2.s3_object_lock_trial_registry import S3ObjectLockTrialRegistryTarget

REPO = Path(__file__).parents[1]


def test_census_intake_requires_every_independent_source_and_stays_incomplete() -> None:
    sources = tuple(HistoricalTrialCensusIntake(kind, chr(97 + index) * 64, False, None) for index, kind in enumerate(HISTORICAL_CENSUS_SOURCES))
    plan = build_historical_trial_census_intake_plan(sources)
    assert plan["completion"]["exact_census_complete"] is False
    assert plan["authorities"]["external_repository_access"] is False
    with pytest.raises(ContractError, match="every source kind"):
        build_historical_trial_census_intake_plan(sources[:-1])


def test_s3_checklist_validates_proposed_target_without_aws_activity() -> None:
    target = S3ObjectLockTrialRegistryTarget(bucket="swing-model-trial-registry", region="us-east-1", prefix="us-stocks-swing-v2", version_id="version-1")
    checklist = build_s3_object_lock_provisioning_checklist(repository_root=REPO, proposed_target=target, aws_account_id="123456789012", bucket_policy_sha256="f" * 64)
    assert checklist["current_status"] == "BACKEND_SELECTED_NOT_CONFIGURED"
    assert checklist["authorities"]["aws_calls"] == 0
    assert checklist["minimum_retention_days"] == 3650
