from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.outcome_firewall import (
    FoundationDataGateway,
    FoundationPhasePolicy,
    OutcomeAccessDenied,
    SYNTHETIC_OUTCOME_SCOPE,
    exploratory_import_violations,
    validate_foundation_payload,
)


NOW = datetime(2026, 8, 13, 22, 0, tzinfo=timezone.utc)


def _root(tmp_path: Path) -> Path:
    for relative in (
        "observations",
        "features",
        "synthetic/outcomes",
        "outcomes",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    (tmp_path / "observations" / "past_returns.json").write_text("{}", encoding="utf-8")
    (tmp_path / "features" / "trend.json").write_text("{}", encoding="utf-8")
    (tmp_path / "synthetic" / "outcomes" / "generated.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "outcomes" / "realized_returns.json").write_text("{}", encoding="utf-8")
    return tmp_path.resolve()


def test_default_gateway_allows_past_inputs_and_denies_outcomes(tmp_path: Path) -> None:
    gateway = FoundationDataGateway(_root(tmp_path))
    allowed = gateway.resolve_foundation_input(
        "observations/past_returns.json",
        purpose="PAST_ONLY_PREDICTOR_INPUT",
        requested_at=NOW,
    )
    assert allowed.name == "past_returns.json"
    with pytest.raises(OutcomeAccessDenied) as denied:
        gateway.resolve_foundation_input(
            "outcomes/realized_returns.json",
            purpose="EXPLORATORY_FEATURE_DEVELOPMENT",
            requested_at=NOW,
        )
    assert denied.value.event.decision == "DENY"
    assert len(gateway.audit_events) == 2
    assert len({event.event_id for event in gateway.audit_events}) == 2


def test_real_outcome_api_is_unconditionally_absent() -> None:
    policy = FoundationPhasePolicy.default()
    assert not policy.real_outcome_access
    assert not policy.holdout_access
    with pytest.raises(ContractError, match="cannot authorize"):
        replace(policy, real_outcome_access=True).validate()


def test_synthetic_outcome_requires_exact_permit_and_namespace(tmp_path: Path) -> None:
    gateway = FoundationDataGateway(_root(tmp_path))
    permit = SyntheticOnlyPermit.create(
        fixture_id="foundation-rehearsal",
        scope=SYNTHETIC_OUTCOME_SCOPE,
    )
    resolved = gateway.resolve_synthetic_outcome_fixture(
        "synthetic/outcomes/generated.json",
        permit=permit,
        requested_at=NOW,
    )
    assert resolved.name == "generated.json"
    assert gateway.audit_events[-1].decision == "ALLOW_SYNTHETIC_OUTCOME"
    wrong = SyntheticOnlyPermit.create(
        fixture_id="wrong-scope",
        scope="SYNTHETIC_SESSION_CALENDAR",
    )
    with pytest.raises(ContractError, match="scope"):
        gateway.resolve_synthetic_outcome_fixture(
            "synthetic/outcomes/generated.json",
            permit=wrong,
            requested_at=NOW,
        )
    with pytest.raises(OutcomeAccessDenied):
        gateway.resolve_foundation_input(
            "synthetic/outcomes/generated.json",
            purpose="EXPLORATORY_FEATURE_DEVELOPMENT",
            requested_at=NOW,
        )


def test_payload_and_import_layers_reject_outcome_poison() -> None:
    validate_foundation_payload(
        {
            "past_return": 0.1,
            "rolling": {"volatility": 0.2},
        }
    )
    with pytest.raises(ContractError, match="prohibited outcome field"):
        validate_foundation_payload(
            {"rolling": {"forward_return": 0.2}}
        )
    assert exploratory_import_violations(
        "from us_stocks_swing_model_v2.outcomes import build_outcome\n"
    ) == ("outcomes",)
    assert exploratory_import_violations(
        "from us_stocks_swing_model_v2.causal_foundation import AvailabilityStamp\n"
    ) == ()


def test_denied_operation_is_a_content_addressed_audit_event(tmp_path: Path) -> None:
    gateway = FoundationDataGateway(_root(tmp_path))
    with pytest.raises(OutcomeAccessDenied) as denied:
        gateway.deny_real_outcome_operation(
            "GENERATE_REAL_FORWARD_LABELS",
            requested_at=NOW,
        )
    event = denied.value.event
    assert event.requested_path == "REAL_OUTCOME_API"
    assert event.decision == "DENY"
    assert gateway.audit_events == (event,)
