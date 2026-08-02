from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import IntegrityError
from us_stocks_swing_model_v2.providers import alpaca_sip_qualification_publisher as publisher


ROOT = Path(__file__).resolve().parents[1]


def _repository(_root: Path) -> dict[str, str]:
    return {"commit": "a" * 40, "tree": "b" * 40}


def _assessment(_root: Path, policy: dict[str, object]) -> dict[str, object]:
    return {
        "assessment_id": policy["assessment_id"],
        "qualification_plan_id": policy["qualification_plan_id"],
        "activation_authorized": False,
        "snapshot": {
            "snapshot_id": policy["snapshot_id"],
            "raw_sha256": policy["raw_sha256"],
        },
        "qualification": {
            "calendar_release_id": policy["calendar_release_id"],
            "state": "PASS",
            "bar_count": 10,
        },
    }


def test_fixed_policy_binds_the_captured_sip_evidence() -> None:
    policy, _ = publisher.load_policy(ROOT)
    assert policy["qualification_plan_id"] == "ec306f803e0d836fa8682d9113c6a861a35411f45b827d6cff82859e14b3daf6"
    assert policy["network_request_plan_id"] == "ca41631ff3b2eb2fb41bc59af1d3b3178edb2a8b72b46ae20329ea84cdbd5963"
    assert policy["assessment_id"] == "484b671f7ae6b3886ee22ec4156a7fb9b7a7dbc88f625616678c3f4dbf0ce52b"
    assert policy["snapshot_id"] == "647b5c9f5e7764eeb77b0fa153b49596e983f23d1c962c9bc19f3919d06faae1"
    assert policy["authorities"]["source_activation"] is False


def test_publication_plan_is_hash_bound_and_non_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publisher, "_repository", _repository)
    monkeypatch.setattr(publisher, "_assessment", _assessment)
    plan = publisher.build_publication_plan(repo_root=ROOT)
    assert plan["network_calls"] == 0
    assert plan["source_activation"] is False
    assert plan["config_sources_mutation"] is False
    assert publisher._validate_plan(plan) == plan["publication_plan_id"]


def test_altered_publication_plan_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publisher, "_repository", _repository)
    monkeypatch.setattr(publisher, "_assessment", _assessment)
    plan = publisher.build_publication_plan(repo_root=ROOT)
    altered = json.loads(json.dumps(plan))
    altered["source_activation"] = True
    with pytest.raises(IntegrityError, match="publication plan"):
        publisher._validate_plan(altered)


def test_receipt_never_authorizes_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publisher, "_repository", _repository)
    monkeypatch.setattr(publisher, "_assessment", _assessment)
    plan = publisher.build_publication_plan(repo_root=ROOT)
    receipt = publisher._receipt(plan, "2026-08-02T00:09:33.720939Z")
    assert receipt["activation_authorized"] is False
    assert receipt["authorities"]["source_activation"] is False
    assert receipt["receipt_id"]
