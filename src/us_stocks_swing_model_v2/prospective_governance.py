"""Non-registering governance templates for the prospective research lane."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .common import canonical_json_bytes, sha256_bytes
from .errors import ContractError
from .git_trial_registry import GitTrialRegistryPolicy


HISTORICAL_CENSUS_SOURCES = (
    "legacy_repository_trial_records",
    "local_project_trial_records",
    "manual_reports_and_plots",
    "external_outcome_exposure_records",
)
PREREGISTRATION_REQUIRED_FIELDS = (
    "hypothesis",
    "data_release_ids",
    "baselines",
    "cost_policy",
    "mees_policy",
    "multiplicity_family",
    "robustness_policy",
)
SLEEVES = ("STOCK_LONG", "STOCK_SHORT", "ETF_LONG", "ETF_SHORT")


def build_historical_trial_census_workflow() -> dict[str, object]:
    """Return the required census workflow without claiming it has been done.

    This deliberately has no local-ledger input.  A local ledger can be one
    inspected source, never the substitute for the independent census.
    """

    unsigned = {
        "schema_version": 1,
        "mode": "HISTORICAL_TRIAL_CENSUS_WORKFLOW_ONLY",
        "required_sources": list(HISTORICAL_CENSUS_SOURCES),
        "conservative_counting": {
            "uncertain_attempts_count": True,
            "manual_plot_or_report_exposure_counts": True,
            "local_ledger_is_sufficient": False,
        },
        "completion": {
            "exact_census_complete": False,
            "status": "INDETERMINATE_BLOCKS_TRUSTED_GATE",
            "training_or_evaluation_authorized": False,
        },
    }
    return {**unsigned, "historical_trial_census_workflow_id": sha256_bytes(canonical_json_bytes(unsigned))}


def build_prospective_preregistration_template(
    *,
    repository_root: Path,
) -> dict[str, object]:
    """Prepare the exact future package shape without choosing a hypothesis.

    The selected registry is a local tracked file backed up to the configured
    GitHub branch. This template never opens outcomes or writes, stages,
    commits, or pushes a registration.
    """

    root = Path(repository_root).resolve(strict=True)
    registry = GitTrialRegistryPolicy.load(
        root / "config/trial_registry_git_policy.json", repository_root=root
    )
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "mode": "PROSPECTIVE_PREREGISTRATION_TEMPLATE_ONLY",
        "required_unselected_fields": list(PREREGISTRATION_REQUIRED_FIELDS),
        "fixed_contract": {
            "feature_schema_id": "prospective_price_only_v1",
            "feature_names": [
                "d0_raw_intraday_return",
                "trailing_5_session_raw_return",
                "trailing_5_session_raw_volatility",
            ],
            "target": "D1_OPEN_TO_D5_CLOSE_SIMPLE_SPLIT_NORMALIZED_PRICE_RETURN",
            "sleeves": list(SLEEVES),
            "wfa": {
                "initial_training_sessions": 1008,
                "outer_folds": 8,
                "outer_test_sessions": 126,
                "final_holdout_sessions": 252,
            },
        },
        "external_registry": {
            "backend": "LOCAL_GIT_WITH_GITHUB_BACKUP",
            "policy_id": registry.policy_id,
            "status": registry.status,
            "owner_controlled": registry.owner_controlled,
            "independent_immutability": registry.independent_immutability,
            "registration_available": registry.status == "CONFIGURED_LOCAL_GIT",
        },
        "authorities": {
            "outcome_access": False,
            "registration": False,
            "training": False,
            "evaluation": False,
            "git_write": False,
            "git_commit": False,
            "git_push": False,
        },
    }
    return {**unsigned, "prospective_preregistration_template_id": sha256_bytes(canonical_json_bytes(unsigned))}
