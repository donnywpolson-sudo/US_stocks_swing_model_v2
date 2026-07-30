from __future__ import annotations

import json
from pathlib import Path

import pytest

import us_stocks_swing_model_v2.cli.prepare_alpaca_source_cutover as cutover_cli
from us_stocks_swing_model_v2.cli.prepare_alpaca_source_cutover import main
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.providers.alpaca_qualification_readiness import (
    _design_from_context,
    _validate_policy_shape,
    load_alpaca_feed_qualification_policy,
)


REPO = Path(__file__).resolve().parents[1]


def test_checked_in_policy_is_content_addressed_and_non_authorizing() -> None:
    policy = load_alpaca_feed_qualification_policy(REPO)
    unsigned = {key: value for key, value in policy.items() if key != "policy_id"}
    assert policy["policy_id"] == sha256_bytes(canonical_json_bytes(unsigned))
    assert policy["assessment"]["assessment_id"] == (
        "3789bb3002d89dcab395a1a4ba6243af028926c37798645069a789a7869ff9e1"
    )
    assert policy["assessment"]["selected_feed_candidate"] == "sip"
    assert policy["receipt_publication"]["source_active"] is False
    assert policy["source_cutover"]["activation_requires_separate_authorization"] is True


def test_policy_rejects_weakened_selection_or_activation() -> None:
    policy = load_alpaca_feed_qualification_policy(REPO)
    weakened = json.loads(json.dumps(policy))
    weakened["assessment"]["selected_feed_candidate"] = "iex"
    with pytest.raises(ContractError, match="assessment binding"):
        _validate_policy_shape(weakened)
    weakened = json.loads(json.dumps(policy))
    weakened["source_cutover"]["activation_requires_separate_authorization"] = False
    with pytest.raises(ContractError, match="cutover contract"):
        _validate_policy_shape(weakened)


def test_design_is_content_addressed_and_grants_no_authority() -> None:
    policy = load_alpaca_feed_qualification_policy(REPO)
    assessment = {
        **policy["assessment"],
        "schema_version": 1,
    }
    design = _design_from_context(
        policy=policy,
        assessment=assessment,
        repository={"head": "a" * 40, "tree": "b" * 40},
    )
    design_id = design.pop("design_id")
    assert design_id == sha256_bytes(canonical_json_bytes(design))
    assert design["selected_feed_candidate"] == "sip"
    assert not any(design["authorities"].values())
    assert design["source_cutover"]["mutations"]["qualified_feed"] == "sip"


def test_design_rejects_nonpassing_or_activation_claiming_assessment() -> None:
    policy = load_alpaca_feed_qualification_policy(REPO)
    for change in (
        {"selected_feed_candidate": None},
        {"selection_reason": "neither_pass"},
        {"activation_authorized": True},
    ):
        assessment = {**policy["assessment"], **change}
        with pytest.raises(ContractError, match="does not satisfy"):
            _design_from_context(
                policy=policy,
                assessment=assessment,
                repository={"head": "a" * 40, "tree": "b" * 40},
            )


def test_cli_is_plan_only_no_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    design = {
        "mode": "ALPACA_QUALIFICATION_RECEIPT_AND_CUTOVER_PLAN_ONLY_NO_WRITES",
        "design_id": "a" * 64,
        "authorities": {"source_activation": False},
    }
    monkeypatch.setattr(
        cutover_cli,
        "build_alpaca_feed_cutover_design",
        lambda _root: design,
    )
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))
    assert main([]) == 0
    assert json.loads(capsys.readouterr().out) == design
    assert tuple(tmp_path.rglob("*")) == before


def test_planner_has_no_network_or_publication_transport() -> None:
    source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "providers"
        / "alpaca_qualification_readiness.py"
    ).read_text(encoding="utf-8")
    cli_source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "cli"
        / "prepare_alpaca_source_cutover.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("urllib", "open_without_redirects", "--execute", "atomic_write"):
        assert forbidden not in source
        assert forbidden not in cli_source
