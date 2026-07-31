from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import IntegrityError
import us_stocks_swing_model_v2.cli.plan_alpaca_historical_backfill_publication as cli
from us_stocks_swing_model_v2.providers.alpaca_historical_backfill_publication import (
    DATASET,
    MODE,
    QUALITY_STATE,
    build_historical_backfill_publication_plan_from_corpus,
    load_historical_backfill_publication_policy,
    publication_plan_summary,
)


REPO = Path(__file__).resolve().parents[1]
CREATED_AT = "2026-07-31T20:00:00Z"


def _complete_corpus() -> dict[str, object]:
    unsigned = {
        "schema_version": 1,
        "plan_type": "ALPACA_SIP_HISTORICAL_BACKFILL_COMPLETE_CORPUS",
        "mode": "SYNTHETIC_NO_WRITE",
        "backfill_plan_id": "1" * 64,
        "repository": {
            "root": str(REPO),
            "branch": "main",
            "commit": "2" * 40,
            "tree": "3" * 40,
        },
        "policy_id": "4" * 64,
        "network_registry_id": "5" * 64,
        "group_count": 19,
        "unit_count": 1045,
        "page_count": 1914,
        "raw_bytes": 1400072168,
        "group_continuation_ids_sha256": "6" * 64,
        "unit_assessment_ids_sha256": "7" * 64,
        "selected_snapshot_ids_sha256": "8" * 64,
        "page_evidence_census_sha256": "9" * 64,
        "group_continuation_ids": ["a" * 64],
        "page_evidence": [],
        "evidence_boundary": {
            "evidence_class": "LEGACY_DISCOVERY",
            "quality_state": QUALITY_STATE,
            "historical_membership_proven": False,
            "survivorship_safe": False,
            "may_support_confirmation": False,
            "hfdl_included": False,
        },
        "authorities": {
            "provider_access": False,
            "credential_access": False,
            "snapshot_write": False,
            "publication": False,
            "activation": False,
            "research": False,
        },
        "stop_conditions": ["synthetic"],
    }
    return {
        **unsigned,
        "complete_corpus_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def test_checked_in_policy_is_plan_only_and_non_authorizing() -> None:
    policy, policy_id = load_historical_backfill_publication_policy(REPO)

    assert policy_id == sha256_bytes(canonical_json_bytes(policy))
    assert policy["mode"] == MODE
    assert policy["release_contract"]["dataset"] == DATASET
    assert policy["release_contract"]["quality_state"] == QUALITY_STATE
    assert policy["implementation"]["publication_execution_implemented"] is False
    assert all(value is False for value in policy["authorities"].values())


def test_publication_plan_is_deterministic_and_defers_release_identity(
    tmp_path: Path,
) -> None:
    policy, policy_id = load_historical_backfill_publication_policy(REPO)
    kwargs = {
        "complete_corpus": _complete_corpus(),
        "policy": policy,
        "publication_policy_id": policy_id,
        "accepted_root": (tmp_path / "accepted").resolve(),
        "work_root": (tmp_path / "work").resolve(),
        "created_at": CREATED_AT,
        "code_closure_sha256": "b" * 64,
        "config_closure_sha256": "c" * 64,
        "environment_id": "d" * 64,
    }

    first = build_historical_backfill_publication_plan_from_corpus(**kwargs)
    second = build_historical_backfill_publication_plan_from_corpus(**kwargs)

    assert first == second
    assert publication_plan_summary(first)["publication_plan_id"] == first[
        "publication_plan_id"
    ]
    assert first["prospective_release"]["release_id"] is None
    assert "DEFERRED" in first["prospective_release"]["release_id_disposition"]
    assert first["implementation"]["release_builder_implemented"] is False
    assert not any(first["authorities"].values())


def test_publication_plan_rejects_tampered_completeness_identity(
    tmp_path: Path,
) -> None:
    policy, policy_id = load_historical_backfill_publication_policy(REPO)
    corpus = _complete_corpus()
    corpus["raw_bytes"] = 1

    with pytest.raises(IntegrityError, match="complete-corpus ID differs"):
        build_historical_backfill_publication_plan_from_corpus(
            complete_corpus=corpus,
            policy=policy,
            publication_policy_id=policy_id,
            accepted_root=(tmp_path / "accepted").resolve(),
            work_root=(tmp_path / "work").resolve(),
            created_at=CREATED_AT,
            code_closure_sha256="b" * 64,
            config_closure_sha256="c" * 64,
            environment_id="d" * 64,
        )


def test_cli_emits_summary_without_execution_or_writes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy, policy_id = load_historical_backfill_publication_policy(REPO)
    plan = build_historical_backfill_publication_plan_from_corpus(
        complete_corpus=_complete_corpus(),
        policy=policy,
        publication_policy_id=policy_id,
        accepted_root=(REPO / "data/vault/accepted").resolve(),
        work_root=(REPO / "data/w/alpaca_historical_backfill_publication").resolve(),
        created_at=CREATED_AT,
        code_closure_sha256="b" * 64,
        config_closure_sha256="c" * 64,
        environment_id="d" * 64,
    )
    monkeypatch.setattr(
        cli,
        "build_historical_backfill_publication_plan",
        lambda **_kwargs: plan,
    )

    assert cli.main(["--created-at", CREATED_AT]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "PLAN_ONLY_NO_NETWORK_NO_WRITES"
    assert output["publication_plan"]["publication_plan_id"] == plan[
        "publication_plan_id"
    ]
    assert output["publication_authorized"] is False
    assert output["publication_implemented"] is False
