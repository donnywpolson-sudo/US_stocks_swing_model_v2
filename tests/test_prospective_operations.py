from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.prospective_governance import HISTORICAL_CENSUS_SOURCES
from us_stocks_swing_model_v2.prospective_operations import (
    HistoricalTrialCensusIntake,
    HistoricalTrialCensusLocator,
    build_historical_trial_census_intake_plan,
    build_historical_trial_census_locator_manifest,
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


def test_census_locator_manifest_is_deterministic_bounded_and_no_io() -> None:
    locators = (
        HistoricalTrialCensusLocator.windows_paths(
            "legacy_repository_trial_records",
            (r"C:\Users\donny\Desktop\US_stocks_swing_model",),
        ),
        HistoricalTrialCensusLocator.windows_paths(
            "local_project_trial_records",
            (r"C:\Users\donny\Desktop\US_stocks_swing_model_v2",),
        ),
        HistoricalTrialCensusLocator.windows_paths(
            "manual_reports_and_plots",
            (
                r"C:\Users\donny\Documents\model-reports",
                r"C:\Users\donny\Documents\model-plots",
            ),
        ),
        HistoricalTrialCensusLocator.owner_manifest(
            "external_outcome_exposure_records",
            "d" * 64,
        ),
    )
    manifest = build_historical_trial_census_locator_manifest(locators)
    assert manifest == build_historical_trial_census_locator_manifest(locators)
    assert manifest["completion"]["status"] == "INDETERMINATE_BLOCKS_TRUSTED_GATE"
    assert manifest["authorities"] == {
        "filesystem_discovery": False,
        "source_content_read": False,
        "external_repository_access": False,
        "registration": False,
        "training": False,
        "evaluation": False,
    }
    assert [row["locator_sha256"] for row in manifest["sources"]] == [
        item.locator_sha256 for item in locators
    ]
    with pytest.raises(ContractError, match="every source kind"):
        build_historical_trial_census_locator_manifest(locators[:-1])
    with pytest.raises(ContractError, match="absolute and bounded"):
        HistoricalTrialCensusLocator.windows_paths(
            "legacy_repository_trial_records",
            (r"..\US_stocks_swing_model",),
        )
    with pytest.raises(ContractError, match="one content hash"):
        HistoricalTrialCensusLocator(
            source_kind="external_outcome_exposure_records",
            locator_type="OWNER_MANIFEST_SHA256",
            locator_values=("d" * 64, "e" * 64),
        ).validate()


def test_s3_checklist_validates_proposed_target_without_aws_activity() -> None:
    target = S3ObjectLockTrialRegistryTarget(bucket="swing-model-trial-registry", region="us-east-1", prefix="us-stocks-swing-v2", version_id="version-1")
    checklist = build_s3_object_lock_provisioning_checklist(repository_root=REPO, proposed_target=target, aws_account_id="123456789012", bucket_policy_sha256="f" * 64)
    assert checklist["current_status"] == "BACKEND_SELECTED_NOT_CONFIGURED"
    assert checklist["authorities"]["aws_calls"] == 0
    assert checklist["minimum_retention_days"] == 3650
