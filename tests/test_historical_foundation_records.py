from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sys

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.feature_registry import load_feature_registry
from us_stocks_swing_model_v2.foundation_records import (
    validate_historical_foundation_gate,
    validate_historical_research_protocol,
    validate_historical_source_inventory,
)
from us_stocks_swing_model_v2.foundation_rehearsal import (
    REHEARSAL_STATE,
    execute_synthetic_foundation_rehearsal,
)
from us_stocks_swing_model_v2.outcome_firewall import (
    FoundationPhasePolicy,
    load_foundation_phase_policy,
)


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _deny_io_capabilities():
    active = {"enabled": True}

    def guard(event: str, args: tuple[object, ...]) -> None:
        if active["enabled"] and event in {
            "open",
            "socket.connect",
            "subprocess.Popen",
        }:
            raise AssertionError(
                f"synthetic foundation rehearsal attempted prohibited I/O: {event}"
            )

    sys.addaudithook(guard)
    try:
        yield
    finally:
        active["enabled"] = False


def test_feature_registry_matches_causal_implementation_and_denies_outcome_ranking(
    tmp_path: Path,
) -> None:
    path = ROOT / "config/feature_registry_v1.json"
    registry = load_feature_registry(path)
    assert tuple(feature.name for feature in registry.features) == (
        "d0_raw_intraday_return",
        "trailing_5_session_raw_return",
        "trailing_5_session_raw_volatility",
    )
    assert registry.real_outcome_access is False
    assert registry.performance_based_ranking is False
    assert {feature.status for feature in registry.features} == {"SOURCE_BLOCKED"}

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["real_outcome_access"] = True
    unsigned = dict(payload)
    unsigned.pop("registry_id")
    payload["registry_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    poisoned = tmp_path / "feature_registry.json"
    poisoned.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="outcome access"):
        load_feature_registry(poisoned)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["features"][0]["required_source_fields"] = [
        "available_at",
        "close",
        "future_close",
    ]
    unsigned = dict(payload)
    unsigned.pop("registry_id")
    payload["registry_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    poisoned.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="outcomes or labels"):
        load_feature_registry(poisoned)


def test_outcome_firewall_config_is_exactly_default_deny() -> None:
    configured = load_foundation_phase_policy(
        ROOT / "config/outcome_firewall_v1.json"
    )
    assert configured == FoundationPhasePolicy.default()
    assert configured.real_outcome_access is False
    assert configured.holdout_access is False
    assert configured.trading is False


def test_historical_inventory_and_protocol_are_content_addressed_and_fail_closed() -> None:
    inventory = validate_historical_source_inventory(
        ROOT / "config/historical_source_inventory_v1.json"
    )
    assert inventory["claims"]["source_dependent_readiness"] == "BLOCKED"
    assert any(
        item["source_class"]
        == "HISTORICAL_POINT_IN_TIME_INDEX_OR_UNIVERSE_MEMBERSHIP"
        for item in inventory["unavailable_or_unqualified_source_classes"]
    )
    protocol = validate_historical_research_protocol(
        ROOT / "config/historical_research_protocol_v1.json"
    )
    assert protocol["final_holdout"]["populated"] is False
    assert protocol["authority"]["separate_future_authorization_required"] is True
    assert protocol["unresolved_decisions"]
    gate = validate_historical_foundation_gate(
        ROOT / "config/historical_foundation_readiness_gate_v1.json"
    )
    assert gate["phase_status"] == "PASS_WITH_CAVEATS"
    assert gate["future_real_outcome_phase_eligible"] is False


def test_synthetic_end_to_end_rehearsal_is_deterministic_and_io_free() -> None:
    registry = load_feature_registry(ROOT / "config/feature_registry_v1.json")
    with _deny_io_capabilities():
        first = execute_synthetic_foundation_rehearsal(
            feature_registry=registry,
            seed=20260813,
        )
        second = execute_synthetic_foundation_rehearsal(
            feature_registry=registry,
            seed=20260813,
        )
    assert first == second
    assert first.manifest["state"] == REHEARSAL_STATE
    assert first.manifest["real_market_prices_used"] is False
    assert first.manifest["real_outcomes_used"] is False
    assert first.manifest["in_memory_artifacts_only"] is True
    assert first.manifest["session_embargo"] == 5
    assert first.manifest["outer_fold_count"] == 3
    assert len(first.prediction_artifact_ids) == 3


def test_synthetic_rehearsal_seed_is_bound_into_every_run_manifest() -> None:
    registry = load_feature_registry(ROOT / "config/feature_registry_v1.json")
    first = execute_synthetic_foundation_rehearsal(
        feature_registry=registry,
        seed=20260813,
    )
    changed = execute_synthetic_foundation_rehearsal(
        feature_registry=registry,
        seed=20260814,
    )
    assert changed.seed == 20260814
    assert changed.rehearsal_id != first.rehearsal_id
    assert changed.synthetic_dataset_sha256 != first.synthetic_dataset_sha256
