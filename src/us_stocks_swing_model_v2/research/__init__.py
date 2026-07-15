"""Synthetic-only statistical mechanics for stock/ETF research.

Importing this package performs no I/O, fitting, network access, history reads,
or artifact creation.  Its outputs are mechanics diagnostics, never alpha
evidence or candidate authorization.
"""

from .bootstrap import (
    apply_shared_indices,
    stationary_bootstrap_index_kernel,
    stationary_bootstrap_index_rows,
    stationary_bootstrap_indices,
)
from .artifacts import (
    DIRECTION_SEMANTICS,
    MODEL_KIND,
    DistributionPrediction,
    ExecutorRegistration,
    FoldFitAudit,
    FrozenPredictionArtifact,
    InnerFoldSampleAudit,
    LinearDistributionModel,
)
from .contracts import (
    ResearchArrayBinding,
    ResearchContractError,
    SyntheticOnlyPermit,
    assert_disjoint_partitions,
    make_synthetic_permit,
    require_synthetic_permit,
)
from .controls import (
    NegativeControlOutcome,
    NegativeControlResult,
    NegativeControlState,
    apply_negative_control_indices,
    circular_block_derangement_indices,
    evaluate_negative_controls,
    synthetic_noise_control,
)
from .cscv import CSCVResult, exhaustive_cscv_pbo
from .dsr import DeflatedSharpeResult, deflated_sharpe_ratio
from .evaluator import FoldEvaluation, evaluate_frozen_predictions
from .executor import (
    EXECUTOR_ENTRYPOINT,
    EXECUTOR_MECHANICS_VERSION,
    SyntheticNestedWfaPlan,
    SyntheticResearchDataset,
    SyntheticResearchExecution,
    execute_synthetic_nested_wfa,
    synthetic_fixture_vector,
)
from .hac import HACMeanResult, hac_t_statistic, newey_west_mean
from .multiple_testing import (
    RomanoWolfResult,
    romano_wolf_from_differentials,
    romano_wolf_stepdown,
)
from .power import PowerPlan, training_only_mde
from .sleeves import (
    PortfolioCharter,
    PortfolioState,
    SleeveGateResult,
    SleeveState,
    SleeveThresholds,
    SyntheticSleeveMetrics,
    evaluate_portfolio_mechanics,
    evaluate_synthetic_sleeve,
)
from .splits import (
    InnerFold,
    NestedFold,
    SessionWindow,
    TemporalSamples,
    nested_chronological_splits,
    purge_and_post_embargo_indices,
)

__all__ = [
    "CSCVResult",
    "DIRECTION_SEMANTICS",
    "DeflatedSharpeResult",
    "DistributionPrediction",
    "EXECUTOR_ENTRYPOINT",
    "EXECUTOR_MECHANICS_VERSION",
    "ExecutorRegistration",
    "FoldEvaluation",
    "FoldFitAudit",
    "FrozenPredictionArtifact",
    "HACMeanResult",
    "InnerFold",
    "InnerFoldSampleAudit",
    "LinearDistributionModel",
    "MODEL_KIND",
    "NegativeControlOutcome",
    "NegativeControlResult",
    "NegativeControlState",
    "NestedFold",
    "PortfolioCharter",
    "PortfolioState",
    "PowerPlan",
    "ResearchArrayBinding",
    "ResearchContractError",
    "RomanoWolfResult",
    "SessionWindow",
    "SleeveGateResult",
    "SleeveState",
    "SleeveThresholds",
    "SyntheticOnlyPermit",
    "SyntheticNestedWfaPlan",
    "SyntheticResearchDataset",
    "SyntheticResearchExecution",
    "SyntheticSleeveMetrics",
    "TemporalSamples",
    "apply_negative_control_indices",
    "apply_shared_indices",
    "assert_disjoint_partitions",
    "circular_block_derangement_indices",
    "deflated_sharpe_ratio",
    "evaluate_negative_controls",
    "evaluate_frozen_predictions",
    "evaluate_portfolio_mechanics",
    "evaluate_synthetic_sleeve",
    "exhaustive_cscv_pbo",
    "execute_synthetic_nested_wfa",
    "hac_t_statistic",
    "make_synthetic_permit",
    "nested_chronological_splits",
    "newey_west_mean",
    "purge_and_post_embargo_indices",
    "require_synthetic_permit",
    "romano_wolf_from_differentials",
    "romano_wolf_stepdown",
    "stationary_bootstrap_index_kernel",
    "stationary_bootstrap_index_rows",
    "stationary_bootstrap_indices",
    "synthetic_noise_control",
    "synthetic_fixture_vector",
    "training_only_mde",
]
