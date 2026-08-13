"""Deterministic end-to-end rehearsal using generated fixtures only."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Mapping

import numpy as np

from .alpaca_free_bounded import EvidenceClass
from .bounded_universe import (
    AFFIRMATIVE_COMMON_STOCK,
    IdentityEvidence,
    LiquidityObservation,
    PRIMARY_PROFILE,
    UniverseCandidate,
    build_universe_snapshot,
)
from .capabilities import SyntheticOnlyPermit as FoundationSyntheticPermit
from .causal_foundation import (
    AvailabilityStamp,
    CausalDailyBar,
    SessionBoundary,
    build_causal_stock_date_panel,
)
from .common import canonical_json_bytes, iso_z, require_sha256, sha256_bytes
from .corporate_actions import BitemporalActionLedger, CorporateActionCoverage
from .errors import ContractError, IntegrityError
from .feature_registry import FeatureRegistry
from .identity import BitemporalIdentityLedger, IdentitySnapshot, IdentityVersion
from .outcome_firewall import FoundationPhasePolicy, SYNTHETIC_OUTCOME_SCOPE
from .prospective_price_features import CausalPriceBar, materialize_price_only_features
from .research.artifacts import ExecutorRegistration
from .research.contracts import make_synthetic_permit
from .research.economics import (
    DailyCohortBook,
    EconomicPolicy,
    reconstruct_five_cohort_economics,
)
from .research.executor import (
    SyntheticNestedWfaPlan,
    SyntheticResearchDataset,
    execute_synthetic_nested_wfa,
    synthetic_fixture_vector,
)
from .research.splits import (
    SessionWindow,
    TemporalSamples,
    nested_chronological_splits,
)
from .schemas import SecurityType


UTC = timezone.utc
REHEARSAL_STATE = "PASS_SYNTHETIC_MECHANICS_ONLY"
GENERATOR_ID = "foundation-rehearsal-generated-fixture-v1"
FEATURE_EVIDENCE_STATE = "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
SLEEVES = ("stock_long", "stock_short", "etf_long", "etf_short")


def _hash(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def _weekdays_through(end: date, count: int) -> tuple[date, ...]:
    result: list[date] = []
    cursor = end
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(reversed(result))


def _identity_snapshot(
    permit: FoundationSyntheticPermit,
    *,
    effective_at: datetime,
) -> IdentitySnapshot:
    alpaca_snapshot_id = _hash("synthetic-alpaca-identity-snapshot")
    nasdaq_snapshot_id = _hash("synthetic-nasdaq-membership-snapshot")
    row = IdentityVersion(
        asset_id="synthetic-asset-1",
        symbol="SYN1",
        security_type=SecurityType.STOCK,
        listing_exchange="NYSE",
        active=True,
        eligible=True,
        membership_present=True,
        abstention_reason=None,
        effective_at=effective_at,
        known_at=effective_at,
        identity_snapshot_id="0" * 64,
        alpaca_snapshot_id=alpaca_snapshot_id,
        nasdaq_snapshot_id=nasdaq_snapshot_id,
        nasdaq_file_created_at=effective_at,
        evidence_state=FEATURE_EVIDENCE_STATE,
        synthetic_permit_ids=(permit.permit_id,),
    )
    provisional = IdentitySnapshot(
        snapshot_id="0" * 64,
        effective_at=effective_at,
        known_at=effective_at,
        complete_membership=True,
        alpaca_snapshot_id=alpaca_snapshot_id,
        nasdaq_snapshot_id=nasdaq_snapshot_id,
        nasdaq_file_created_at=effective_at,
        evidence_state=FEATURE_EVIDENCE_STATE,
        synthetic_permit_ids=(permit.permit_id,),
        rows=(row,),
    )
    unsigned = provisional.receipt_dict()
    unsigned.pop("snapshot_id")
    for item in unsigned["rows"]:
        item.pop("identity_snapshot_id")
    snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
    return replace(
        provisional,
        snapshot_id=snapshot_id,
        rows=(replace(row, identity_snapshot_id=snapshot_id),),
    )


def _synthetic_dataset(seed: int) -> SyntheticResearchDataset:
    sample_count = 110
    rng = np.random.default_rng(seed)
    features = rng.normal(size=(sample_count, 3)).astype(np.float64)
    generated_targets = (
        0.02 * features[:, 0]
        - 0.01 * features[:, 1]
        + 0.005 * features[:, 2]
        + rng.normal(scale=0.003, size=sample_count)
    ).astype(np.float64)
    decisions = np.arange(sample_count, dtype=np.int64)
    return SyntheticResearchDataset(
        sample_ids=tuple(f"synthetic-sample-{index:04d}" for index in range(sample_count)),
        feature_names=(
            "d0_raw_intraday_return",
            "trailing_5_session_raw_return",
            "trailing_5_session_raw_volatility",
        ),
        features=features,
        targets=generated_targets,
        temporal_samples=TemporalSamples(
            decision_session=decisions,
            label_start=decisions + 1,
            label_end=decisions + 6,
            label_known_session=decisions + 6,
        ),
    )


def _synthetic_plan() -> SyntheticNestedWfaPlan:
    return SyntheticNestedWfaPlan(
        outer_test_windows=(
            SessionWindow(70, 80),
            SessionWindow(85, 95),
            SessionWindow(100, 110),
        ),
        inner_validation_windows=(
            (SessionWindow(35, 40), SessionWindow(50, 55)),
            (SessionWindow(45, 50), SessionWindow(65, 70)),
            (SessionWindow(60, 65), SessionWindow(80, 85)),
        ),
        session_embargo=5,
        minimum_fit_samples=20,
        minimum_audit_samples=5,
    )


def _synthetic_books() -> tuple[DailyCohortBook, ...]:
    cohort_starts = tuple(range(1, 9))
    books: list[DailyCohortBook] = []
    for session in range(1, 13):
        weights: dict[str, dict[str, float]] = {}
        sleeves: dict[str, str] = {}
        asset_returns: dict[str, float] = {}
        adv: dict[str, None] = {}
        for index, start in enumerate(cohort_starts):
            if not start <= session < start + 5:
                continue
            cohort_id = f"synthetic-cohort-{start:02d}"
            asset_id = f"synthetic-economic-asset-{start:02d}"
            sleeve = SLEEVES[index % len(SLEEVES)]
            sign = -1.0 if sleeve.endswith("_short") else 1.0
            weights[cohort_id] = {asset_id: sign * 0.2}
            sleeves[cohort_id] = sleeve
            asset_returns[asset_id] = float(((session + start) % 7) - 3) / 1000.0
            adv[asset_id] = None
        books.append(
            DailyCohortBook(
                session=session,
                cohort_weights=weights,
                cohort_sleeves=sleeves,
                asset_returns=asset_returns,
                asset_adv_notional=adv,
            )
        )
    return tuple(books)


@dataclass(frozen=True)
class SyntheticFoundationRehearsal:
    seed: int
    generator_id: str
    fixture_id: str
    feature_registry_id: str
    firewall_policy_id: str
    identity_snapshot_id: str
    universe_snapshot_id: str
    panel_id: str
    feature_result_id: str
    split_result_id: str
    executor_registration_id: str
    prediction_artifact_ids: tuple[str, ...]
    synthetic_execution_id: str
    economic_reconstruction_id: str
    synthetic_dataset_sha256: str
    synthetic_permit_ids: tuple[str, ...]
    manifest: Mapping[str, object]
    rehearsal_id: str

    def validate(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ContractError("synthetic rehearsal seed must be a nonnegative integer")
        if self.generator_id != GENERATOR_ID or not self.fixture_id.startswith("synthetic-"):
            raise ContractError("synthetic rehearsal generator identity differs")
        for field in (
            "feature_registry_id",
            "firewall_policy_id",
            "identity_snapshot_id",
            "universe_snapshot_id",
            "panel_id",
            "feature_result_id",
            "split_result_id",
            "executor_registration_id",
            "synthetic_execution_id",
            "economic_reconstruction_id",
            "synthetic_dataset_sha256",
            "rehearsal_id",
        ):
            require_sha256(getattr(self, field), f"synthetic_rehearsal.{field}")
        if (
            not self.prediction_artifact_ids
            or self.prediction_artifact_ids
            != tuple(sorted(set(self.prediction_artifact_ids)))
        ):
            raise ContractError("synthetic prediction artifact IDs must be sorted and unique")
        for value in (*self.prediction_artifact_ids, *self.synthetic_permit_ids):
            require_sha256(value, "synthetic rehearsal content ID")
        if self.synthetic_permit_ids != tuple(sorted(set(self.synthetic_permit_ids))):
            raise ContractError("synthetic rehearsal permit IDs must be sorted and unique")
        if type(self.manifest) is not dict:
            raise ContractError("synthetic rehearsal manifest must be an exact dict")
        if (
            self.manifest.get("state") != REHEARSAL_STATE
            or self.manifest.get("source_kind") != "GENERATED_SYNTHETIC_ONLY"
            or self.manifest.get("real_market_prices_used") is not False
            or self.manifest.get("real_outcomes_used") is not False
            or self.manifest.get("alpha_evidence") is not False
            or self.manifest.get("candidate_eligible") is not False
            or self.manifest.get("writes_performed") is not False
        ):
            raise ContractError("synthetic rehearsal manifest loses its safety boundary")
        unsigned = dict(self.manifest)
        if unsigned.get("rehearsal_id") != self.rehearsal_id:
            raise IntegrityError("synthetic rehearsal manifest ID differs")
        unsigned.pop("rehearsal_id")
        if self.rehearsal_id != sha256_bytes(canonical_json_bytes(unsigned)):
            raise IntegrityError("synthetic rehearsal ID differs from its manifest")


def execute_synthetic_foundation_rehearsal(
    *,
    feature_registry: FeatureRegistry,
    seed: int = 20260813,
) -> SyntheticFoundationRehearsal:
    """Exercise the complete mechanics boundary without reading external data."""

    feature_registry.validate()
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ContractError("synthetic rehearsal seed must fit uint32")
    d0 = date(2026, 8, 11)
    d1 = date(2026, 8, 12)
    d0_open = datetime(2026, 8, 11, 13, 30, tzinfo=UTC)
    d0_close = datetime(2026, 8, 11, 20, 0, tzinfo=UTC)
    signal_cutoff = d0_close + timedelta(minutes=10)
    fixture_id = f"synthetic-foundation-{seed}"

    identity_permit = FoundationSyntheticPermit.create(
        fixture_id=fixture_id,
        scope="SYNTHETIC_IDENTITY_LEDGER",
    )
    identity_ledger = BitemporalIdentityLedger(synthetic_permit=identity_permit)
    identity_snapshot = _identity_snapshot(
        identity_permit,
        effective_at=d0_close - timedelta(hours=1),
    )
    identity_ledger.append_snapshot(identity_snapshot)

    liquidity_sessions = _weekdays_through(d0 - timedelta(days=1), 60)
    identity_evidence = IdentityEvidence(
        stable_asset_id="synthetic-asset-1",
        provider_asset_id="synthetic-provider-asset-1",
        original_requested_ticker="SYN1",
        returned_ticker="SYN1",
        source_ticker="SYN1",
        requested_as_of=liquidity_sessions[-1],
        ticker_effective_from=date(2020, 1, 2),
        ticker_effective_through=None,
        listing_from=date(2020, 1, 2),
        delisting_through=None,
        exchange="NYSE",
        effective_at=datetime(2020, 1, 2, tzinfo=UTC),
        known_at=d0_close - timedelta(hours=1),
        mapping_evidence_id=_hash("synthetic-mapping-evidence"),
        mapping_status="CONFIRMED_CONTINUITY",
        evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
    )
    candidate = UniverseCandidate(
        identity=identity_evidence,
        ticker="SYN1",
        security_classification=AFFIRMATIVE_COMMON_STOCK,
        exchange="NYSE",
        source_memberships=("SYNTHETIC_DATED_MEMBERSHIP",),
        source_receipt_times=(d0_close - timedelta(hours=1),),
        observations=tuple(
            LiquidityObservation(
                session=session,
                close=20.0 + index / 100.0,
                volume=100_000.0 + index,
                available_at=d0_close - timedelta(hours=2),
                source_hash=_hash(f"synthetic-liquidity-{index}"),
            )
            for index, session in enumerate(liquidity_sessions)
        ),
        evidence_hashes=(_hash("synthetic-universe-evidence"),),
    )
    universe = build_universe_snapshot(
        profile_id=PRIMARY_PROFILE,
        signal_session=d0,
        information_cutoff_session=liquidity_sessions[-1],
        decision_at=d0_close + timedelta(minutes=5),
        candidates=(candidate,),
    )

    action_permit = FoundationSyntheticPermit.create(
        fixture_id=fixture_id,
        scope="SYNTHETIC_CORPORATE_ACTION_LEDGER",
    )
    coverage = CorporateActionCoverage.create(
        effective_start_session=d0,
        effective_end_session=d0,
        asset_scope="EXACT_ASSET_IDS",
        asset_ids=("synthetic-asset-1",),
        received_at=d0_close - timedelta(hours=1),
        source_snapshot_ids=(_hash("synthetic-action-snapshot"),),
        provider_coverage_id=_hash("synthetic-provider-action-coverage"),
        source_release_id=action_permit.permit_id,
        source_epoch="SYNTHETIC_ONLY",
    )
    action_ledger = BitemporalActionLedger(
        synthetic_permit=action_permit,
        coverage=(coverage,),
    )

    calendar_release_id = _hash("synthetic-xnys-calendar")
    current_session = SessionBoundary.create(
        session=d0,
        open_at=d0_open,
        close_at=d0_close,
        early_close=False,
        calendar_release_id=calendar_release_id,
    )
    next_session = SessionBoundary.create(
        session=d1,
        open_at=datetime(2026, 8, 12, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 8, 12, 20, 0, tzinfo=UTC),
        early_close=False,
        calendar_release_id=calendar_release_id,
    )
    availability = AvailabilityStamp(
        effective_time=d0_close,
        published_time=d0_close + timedelta(minutes=2),
        received_time=d0_close + timedelta(minutes=4),
        usable_time=d0_close + timedelta(minutes=5),
        source_revision="synthetic-v1",
        source_identifier="synthetic-daily-bar-generator",
        source_snapshot_id=_hash("synthetic-daily-bar-snapshot"),
    )
    bar = CausalDailyBar.create(
        stable_security_id="synthetic-asset-1",
        session=d0,
        open=25.0,
        high=26.0,
        low=24.5,
        close=25.5,
        volume=250_000,
        trade_count=500,
        vwap=25.25,
        availability=availability,
        source_release_id=_hash("synthetic-bar-release"),
        identity_snapshot_id=identity_snapshot.snapshot_id,
        adjustment_state="RAW_OBSERVED",
        raw_source_bar_id=None,
        corporate_action_ids=(),
        quality_flags=(),
        evidence_state=FEATURE_EVIDENCE_STATE,
    )
    panel = build_causal_stock_date_panel(
        session=current_session,
        next_session=next_session,
        signal_cutoff=signal_cutoff,
        identity_ledger=identity_ledger,
        universe_snapshot=universe,
        bars=(bar,),
        action_ledger=action_ledger,
        evidence_state=FEATURE_EVIDENCE_STATE,
    )

    feature_sessions = _weekdays_through(d0, 6)
    feature_rows = materialize_price_only_features(
        tuple(
            CausalPriceBar(
                asset_id="synthetic-asset-1",
                session=session,
                open=20.0 + index,
                close=20.5 + index,
                available_at=(
                    d0_close + timedelta(minutes=5)
                    if session == d0
                    else d0_close - timedelta(hours=1)
                ),
            )
            for index, session in enumerate(feature_sessions)
        ),
        sessions=feature_sessions,
        decision_session=d0,
        decision_at=signal_cutoff,
        action_coverage_complete=True,
        action_or_delisting_sessions=frozenset(),
    )
    if len(feature_rows) != 1 or feature_rows[0].values is None:
        raise IntegrityError("synthetic causal feature materialization did not complete")
    if tuple(feature_rows[0].values) != tuple(
        feature.name for feature in feature_registry.features
    ):
        raise IntegrityError("synthetic feature result differs from the frozen registry")
    feature_result_id = sha256_bytes(
        canonical_json_bytes(
            {
                "asset_id": feature_rows[0].asset_id,
                "decision_session": feature_rows[0].decision_session.isoformat(),
                "feature_available_at": iso_z(feature_rows[0].feature_available_at),
                "values": feature_rows[0].values,
                "status": feature_rows[0].status,
            }
        )
    )

    dataset = _synthetic_dataset(seed)
    plan = _synthetic_plan()
    folds = nested_chronological_splits(
        dataset.temporal_samples,
        plan.outer_test_windows,
        plan.inner_validation_windows,
        session_embargo=plan.session_embargo,
        minimum_fit_samples=plan.minimum_fit_samples,
        minimum_audit_samples=plan.minimum_audit_samples,
    )
    split_result_id = sha256_bytes(
        canonical_json_bytes(
            {
                "plan": plan.as_dict(),
                "folds": [
                    {
                        "fit_indices": fold.fit_indices.tolist(),
                        "audit_indices": fold.audit_indices.tolist(),
                        "inner": [
                            {
                                "fit_indices": inner.fit_indices.tolist(),
                                "audit_indices": inner.audit_indices.tolist(),
                            }
                            for inner in fold.inner_folds
                        ],
                    }
                    for fold in folds
                ],
            }
        )
    )
    research_permit = make_synthetic_permit(
        synthetic_fixture_vector(dataset),
        generator_id=GENERATOR_ID,
        seed=seed,
    )
    registration = ExecutorRegistration.create(
        feature_schema_id=feature_registry.registry_id,
        feature_names=dataset.feature_names,
        ridge_alphas=(0.01, 0.1, 1.0, 10.0),
        neutral_band=0.005,
        uncertainty_floor=0.001,
    )
    execution = execute_synthetic_nested_wfa(
        dataset,
        permit=research_permit,
        registration=registration,
        plan=plan,
    )
    economics = reconstruct_five_cohort_economics(
        _synthetic_books(),
        policy=EconomicPolicy(),
    )
    firewall = FoundationPhasePolicy.default()
    synthetic_outcome_permit = FoundationSyntheticPermit.create(
        fixture_id=fixture_id,
        scope=SYNTHETIC_OUTCOME_SCOPE,
    )
    permit_ids = tuple(
        sorted(
            (
                identity_permit.permit_id,
                action_permit.permit_id,
                synthetic_outcome_permit.permit_id,
            )
        )
    )
    prediction_ids = tuple(
        sorted(artifact.artifact_id for artifact in execution.prediction_artifacts)
    )
    unsigned_manifest = {
        "schema_version": 1,
        "state": REHEARSAL_STATE,
        "source_kind": "GENERATED_SYNTHETIC_ONLY",
        "generator_id": GENERATOR_ID,
        "seed": seed,
        "fixture_id": fixture_id,
        "feature_registry_id": feature_registry.registry_id,
        "firewall_policy_id": firewall.policy_id,
        "identity_snapshot_id": identity_snapshot.snapshot_id,
        "universe_snapshot_id": universe.snapshot_id,
        "panel_id": panel.panel_id,
        "feature_result_id": feature_result_id,
        "split_result_id": split_result_id,
        "executor_registration_id": registration.registration_id,
        "prediction_artifact_ids": list(prediction_ids),
        "synthetic_execution_id": execution.execution_id,
        "economic_reconstruction_id": economics.reconstruction_id,
        "synthetic_dataset_sha256": research_permit.dataset_sha256,
        "synthetic_permit_ids": list(permit_ids),
        "session_embargo": plan.session_embargo,
        "outer_fold_count": len(folds),
        "in_memory_artifacts_only": True,
        "writes_performed": False,
        "real_market_prices_used": False,
        "real_outcomes_used": False,
        "real_labels_used": False,
        "real_history_authorized": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
        "performance_conclusion_allowed": False,
    }
    rehearsal_id = sha256_bytes(canonical_json_bytes(unsigned_manifest))
    manifest = {**unsigned_manifest, "rehearsal_id": rehearsal_id}
    result = SyntheticFoundationRehearsal(
        seed=seed,
        generator_id=GENERATOR_ID,
        fixture_id=fixture_id,
        feature_registry_id=feature_registry.registry_id,
        firewall_policy_id=firewall.policy_id,
        identity_snapshot_id=identity_snapshot.snapshot_id,
        universe_snapshot_id=universe.snapshot_id,
        panel_id=panel.panel_id,
        feature_result_id=feature_result_id,
        split_result_id=split_result_id,
        executor_registration_id=registration.registration_id,
        prediction_artifact_ids=prediction_ids,
        synthetic_execution_id=execution.execution_id,
        economic_reconstruction_id=economics.reconstruction_id,
        synthetic_dataset_sha256=research_permit.dataset_sha256,
        synthetic_permit_ids=permit_ids,
        manifest=manifest,
        rehearsal_id=rehearsal_id,
    )
    result.validate()
    return result
