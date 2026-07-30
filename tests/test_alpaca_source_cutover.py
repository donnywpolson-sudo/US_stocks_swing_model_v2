from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
import us_stocks_swing_model_v2.cli.activate_alpaca_source as cutover_cli
from us_stocks_swing_model_v2.cli.activate_alpaca_source import main as cutover_main
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.providers.alpaca_qualification_publisher import (
    FIXTURE_SCOPE as PUBLICATION_FIXTURE_SCOPE,
    build_alpaca_qualification_publication_fixture_plan,
    publish_alpaca_qualification_fixture,
)
from us_stocks_swing_model_v2.providers.alpaca_qualification_readiness import (
    load_alpaca_feed_qualification_policy,
)
import us_stocks_swing_model_v2.providers.alpaca_source_cutover as cutover
from us_stocks_swing_model_v2.providers.alpaca_source_cutover import (
    ACTIVATION_CONFIRMATION_TOKEN,
    FIXTURE_SCOPE,
    activate_alpaca_source,
    apply_alpaca_source_cutover_fixture,
    build_alpaca_source_cutover_fixture_plan,
)


REPO = Path(__file__).resolve().parents[1]


def _publication_clock() -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        permit=SyntheticOnlyPermit.create(
            fixture_id="alpaca-cutover-publication-clock",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _fixture_context(tmp_path: Path):
    policy = load_alpaca_feed_qualification_policy(REPO)
    assessment = {
        "assessment_id": policy["assessment"]["assessment_id"],
        "selected_feed_candidate": "sip",
        "selection_reason": "both_pass_prefer_sip",
        "activation_authorized": False,
        "snapshots": {
            feed: {
                "snapshot_id": snapshot["snapshot_id"],
                "raw_sha256": snapshot["raw_sha256"],
                "retrieved_at": "2026-07-30T12:00:00Z",
            }
            for feed, snapshot in policy["snapshots"].items()
        },
        "qualifications": {
            feed: {
                "feed": feed,
                "state": "PASS",
                "reasons": [],
                "snapshot_ids": [snapshot["snapshot_id"]],
                "bar_count": 10,
                "calendar_release_id": policy["calendar"]["release_id"],
                "evidence_state": "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
                "trust_eligible": False,
            }
            for feed, snapshot in policy["snapshots"].items()
        },
    }
    publication_permit = SyntheticOnlyPermit.create(
        fixture_id="alpaca-cutover-publication",
        scope=PUBLICATION_FIXTURE_SCOPE,
    )
    publication_plan = build_alpaca_qualification_publication_fixture_plan(
        policy=policy,
        assessment=assessment,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "publication-work",
        permit=publication_permit,
    )
    publication = publish_alpaca_qualification_fixture(
        plan=publication_plan,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "publication-work",
        clock=_publication_clock(),
        permit=publication_permit,
    )
    config_path = tmp_path / "config" / "sources.json"
    config_path.parent.mkdir()
    shutil.copyfile(REPO / "config" / "sources.json", config_path)
    cutover_permit = SyntheticOnlyPermit.create(
        fixture_id="alpaca-source-cutover",
        scope=FIXTURE_SCOPE,
    )
    plan = build_alpaca_source_cutover_fixture_plan(
        source_config_path=config_path,
        release_directory=publication.release_directory,
        accepted_root=tmp_path / "accepted",
        permit=cutover_permit,
        repo_root=REPO,
    )
    return policy, publication, config_path, cutover_permit, plan


def test_fixture_plan_is_content_addressed_and_non_authorizing(
    tmp_path: Path,
) -> None:
    _, publication, _, _, plan = _fixture_context(tmp_path)
    plan_id = plan.pop("activation_plan_id")
    assert plan_id == sha256_bytes(canonical_json_bytes(plan))
    assert plan["qualification_release"]["release_id"] == publication.release_id
    assert plan["mutations"]["qualified_feed"] == "sip"
    assert plan["activation_count"] == 1
    assert plan["config_file_mutations"] == 1
    assert not any(plan["authorities"].values())


def test_fixture_cutover_changes_only_declared_source_fields(tmp_path: Path) -> None:
    policy, publication, config_path, permit, plan = _fixture_context(tmp_path)
    baseline = json.loads((REPO / "config" / "sources.json").read_text("utf-8"))
    result = apply_alpaca_source_cutover_fixture(
        plan=plan,
        source_config_path=config_path,
        release_directory=publication.release_directory,
        accepted_root=tmp_path / "accepted",
        permit=permit,
        repo_root=REPO,
    )
    activated = json.loads(config_path.read_text("utf-8"))
    source_key = policy["source_cutover"]["source_key"]
    expected = json.loads(json.dumps(baseline))
    expected_source = expected["sources"][source_key]
    expected_source["enabled_for_active_pipeline"] = True
    expected_source["request_contract"]["qualified_feed"] = "sip"
    expected_source["qualification_receipt"] = (
        "data/vault/accepted/alpaca_feed_qualification/"
        f"{publication.release_id}/alpaca_feed_qualification_receipt.json"
    )
    expected_source["status"] = "active_sip_qualified_pending_canonical_bars"
    assert activated == expected
    assert result.source_config_sha256 == plan["source_config_after_sha256"]
    assert activated["sources"][source_key]["request_contract"]["asof"] is None
    assert activated["sources"][source_key]["request_contract"]["adjustment"] == "raw"
    assert b'"qualification_candidates": ["sip", "iex"]' in config_path.read_bytes()


def test_fixture_cutover_is_one_shot_and_rejects_config_drift(tmp_path: Path) -> None:
    _, publication, config_path, permit, plan = _fixture_context(tmp_path)
    apply_alpaca_source_cutover_fixture(
        plan=plan,
        source_config_path=config_path,
        release_directory=publication.release_directory,
        accepted_root=tmp_path / "accepted",
        permit=permit,
        repo_root=REPO,
    )
    with pytest.raises(IntegrityError, match="changed after planning"):
        apply_alpaca_source_cutover_fixture(
            plan=plan,
            source_config_path=config_path,
            release_directory=publication.release_directory,
            accepted_root=tmp_path / "accepted",
            permit=permit,
            repo_root=REPO,
        )


def test_fixture_cutover_rejects_wrong_permit_and_release_tampering(
    tmp_path: Path,
) -> None:
    _, publication, config_path, permit, plan = _fixture_context(tmp_path)
    wrong = SyntheticOnlyPermit.create(
        fixture_id="wrong-alpaca-cutover",
        scope=FIXTURE_SCOPE,
    )
    with pytest.raises(ContractError, match="differs from its permit"):
        apply_alpaca_source_cutover_fixture(
            plan=plan,
            source_config_path=config_path,
            release_directory=publication.release_directory,
            accepted_root=tmp_path / "accepted",
            permit=wrong,
            repo_root=REPO,
        )
    receipt_path = (
        publication.release_directory / "alpaca_feed_qualification_receipt.json"
    )
    receipt = json.loads(receipt_path.read_text("utf-8"))
    receipt["selected_feed"] = "iex"
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(IntegrityError, match="payload hash mismatch"):
        apply_alpaca_source_cutover_fixture(
            plan=plan,
            source_config_path=config_path,
            release_directory=publication.release_directory,
            accepted_root=tmp_path / "accepted",
            permit=permit,
            repo_root=REPO,
        )
    assert config_path.read_bytes() == (REPO / "config" / "sources.json").read_bytes()


def test_production_plan_builder_is_no_write_and_binds_exact_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_alpaca_feed_qualification_policy(REPO)
    repository = {"head": "a" * 40, "tree": "b" * 40}
    release = {
        "release_id": cutover.QUALIFICATION_RELEASE_ID,
        "receipt_id": cutover.QUALIFICATION_RECEIPT_ID,
        "publication_plan_id": cutover.QUALIFICATION_PUBLICATION_PLAN_ID,
        "receipt_file_sha256": cutover.QUALIFICATION_RECEIPT_FILE_SHA256,
        "manifest_file_sha256": cutover.QUALIFICATION_MANIFEST_FILE_SHA256,
    }
    receipt = {
        "policy_id": policy["policy_id"],
        "config_closure": {
            "files": [
                {
                    "path": "config/sources.json",
                    "sha256": policy["source_config_baseline"]["file_sha256"],
                }
            ]
        },
    }
    monkeypatch.setattr(cutover, "_repository_binding", lambda root: repository)
    monkeypatch.setattr(cutover, "_release_binding", lambda *args, **kwargs: (receipt, release))
    entries = [{"path": "synthetic/file", "sha256": "c" * 64}]
    closure = {
        "files": entries,
        "closure_sha256": sha256_bytes(canonical_json_bytes(entries)),
    }
    monkeypatch.setattr(cutover, "_closure", lambda *args: closure)
    monkeypatch.setattr(cutover, "validate_environment_lock", lambda path: "d" * 64)
    plan = cutover.build_alpaca_source_cutover_plan(repo_root=REPO)
    plan_id = plan.pop("activation_plan_id")
    assert plan_id == sha256_bytes(canonical_json_bytes(plan))
    assert plan["qualification_release"] == release
    assert plan["source_config_before_sha256"] == (
        policy["source_config_baseline"]["file_sha256"]
    )
    assert plan["source_config_after_sha256"] != plan["source_config_before_sha256"]


def test_production_activation_rejects_authority_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {"activation_plan_id": "a" * 64}
    monkeypatch.setattr(
        cutover,
        "build_alpaca_source_cutover_plan",
        lambda **kwargs: plan,
    )
    with pytest.raises(PermissionError, match="owner confirmation"):
        activate_alpaca_source(
            approved_plan_id="a" * 64,
            owner_confirmation="NO",
            repo_root=REPO,
        )
    with pytest.raises(PermissionError, match="approved.*differs"):
        activate_alpaca_source(
            approved_plan_id="b" * 64,
            owner_confirmation="YES",
            repo_root=REPO,
        )
    assert tuple(tmp_path.rglob("*")) == ()


def test_cli_defaults_to_plan_only_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = {"activation_plan_id": "a" * 64}
    monkeypatch.setattr(
        cutover_cli,
        "build_alpaca_source_cutover_plan",
        lambda: plan,
    )
    monkeypatch.setattr(
        cutover_cli,
        "activate_alpaca_source",
        lambda **kwargs: pytest.fail("plan-only CLI attempted activation"),
    )
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))
    assert cutover_main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "PLAN_ONLY_NO_WRITES"
    assert output["activation_authorized"] is False
    assert output["config_file_mutations"] == 0
    assert output["canonical_bars"] is False
    assert tuple(tmp_path.rglob("*")) == before


def test_cli_execute_requires_confirmation_and_exact_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cutover_cli,
        "build_alpaca_source_cutover_plan",
        lambda: {"activation_plan_id": "a" * 64},
    )
    monkeypatch.delenv(ACTIVATION_CONFIRMATION_TOKEN, raising=False)
    with pytest.raises(PermissionError, match=ACTIVATION_CONFIRMATION_TOKEN):
        cutover_main(["--execute", "--approved-plan-id", "a" * 64])
    monkeypatch.setenv(ACTIVATION_CONFIRMATION_TOKEN, "YES")
    with pytest.raises(PermissionError, match="approved.*differs"):
        cutover_main(["--execute", "--approved-plan-id", "b" * 64])


def test_cutover_has_no_network_secret_or_canonical_bar_transport() -> None:
    source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "providers"
        / "alpaca_source_cutover.py"
    ).read_text("utf-8")
    cli_source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "cli"
        / "activate_alpaca_source.py"
    ).read_text("utf-8")
    for forbidden in (
        "urllib",
        "open_without_redirects",
        "execute_network",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "api.env",
        "canonicalize",
    ):
        assert forbidden not in source
        assert forbidden not in cli_source
