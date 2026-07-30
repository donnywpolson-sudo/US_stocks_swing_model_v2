from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
import us_stocks_swing_model_v2.cli.publish_alpaca_qualification as publication_cli
from us_stocks_swing_model_v2.cli.publish_alpaca_qualification import (
    main as publication_main,
)
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
import us_stocks_swing_model_v2.providers.alpaca_qualification_publisher as publisher
from us_stocks_swing_model_v2.providers.alpaca_qualification_publisher import (
    FIXTURE_SCOPE,
    PRODUCTION_STATUS,
    PUBLICATION_CONFIRMATION_TOKEN,
    SYNTHETIC_STATUS,
    build_alpaca_qualification_publication_fixture_plan,
    publish_alpaca_qualification_fixture,
    publish_alpaca_qualification_receipt,
    verify_alpaca_qualification_release,
)
from us_stocks_swing_model_v2.providers.alpaca_qualification_readiness import (
    load_alpaca_feed_qualification_policy,
)


REPO = Path(__file__).resolve().parents[1]


def _permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="alpaca-qualification-publication",
        scope=FIXTURE_SCOPE,
    )


def _clock() -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        permit=SyntheticOnlyPermit.create(
            fixture_id="alpaca-qualification-publication-clock",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _synthetic_context(tmp_path: Path):
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
    permit = _permit()
    plan = build_alpaca_qualification_publication_fixture_plan(
        policy=policy,
        assessment=assessment,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        permit=permit,
    )
    return permit, policy, assessment, plan


def test_checked_in_receipt_contract_is_exact_and_non_active() -> None:
    policy = load_alpaca_feed_qualification_policy(REPO)
    publication = policy["receipt_publication"]
    assert set(publication["receipt_fields"]) == publisher.RECEIPT_FIELDS
    assert publication["dataset"] == publisher.DATASET
    assert publication["status"] == PRODUCTION_STATUS
    assert publication["source_active"] is False
    assert publication["network_calls"] == 0


def test_fixture_plan_is_content_addressed_and_grants_no_activation(
    tmp_path: Path,
) -> None:
    _, _, _, plan = _synthetic_context(tmp_path)
    plan_id = plan.pop("publication_plan_id")
    assert plan_id == sha256_bytes(canonical_json_bytes(plan))
    assert plan["publication_count"] == 1
    assert plan["authorities"]["qualification_receipt_publication"] is False
    assert plan["authorities"]["source_activation"] is False
    assert plan["authorities"]["network_calls"] is False


def test_fixture_plan_rejects_assessment_snapshot_drift(tmp_path: Path) -> None:
    permit, policy, assessment, _ = _synthetic_context(tmp_path)
    changed = json.loads(json.dumps(assessment))
    changed["snapshots"]["sip"]["raw_sha256"] = "0" * 64
    with pytest.raises(IntegrityError, match="sip qualification differs"):
        build_alpaca_qualification_publication_fixture_plan(
            policy=policy,
            assessment=changed,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            permit=permit,
        )


def test_production_plan_builder_is_no_write_and_binds_clean_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, assessment, _ = _synthetic_context(tmp_path)
    production_assessment = json.loads(json.dumps(assessment))
    for result in production_assessment["qualifications"].values():
        result["evidence_state"] = "NETWORK_AS_RECEIVED"
        result["trust_eligible"] = True
    repository = {"head": "f" * 40, "tree": "1" * 40}
    monkeypatch.setattr(publisher, "_repository_binding", lambda root: repository)
    monkeypatch.setattr(
        publisher,
        "load_alpaca_feed_qualification_policy",
        lambda root: policy,
    )
    monkeypatch.setattr(
        publisher,
        "build_alpaca_feed_cutover_design",
        lambda root: {"repository": repository},
    )
    monkeypatch.setattr(
        publisher,
        "load_validated_alpaca_feed_qualification_assessment",
        lambda root: production_assessment,
    )
    closure = {
        "files": [{"path": "synthetic/file", "sha256": "2" * 64}],
        "closure_sha256": sha256_bytes(
            canonical_json_bytes(
                [{"path": "synthetic/file", "sha256": "2" * 64}]
            )
        ),
    }
    monkeypatch.setattr(publisher, "_closure", lambda *args: closure)
    monkeypatch.setattr(
        publisher,
        "validate_environment_lock",
        lambda path: "3" * 64,
    )
    before = tuple(tmp_path.rglob("*"))
    plan = publisher.build_alpaca_qualification_publication_plan(repo_root=REPO)
    assert plan["publication_plan_id"] == sha256_bytes(
        canonical_json_bytes(
            {key: value for key, value in plan.items() if key != "publication_plan_id"}
        )
    )
    assert plan["mode"] == "PUBLISH_ONE_NON_ACTIVE_ALPACA_QUALIFICATION_RECEIPT"
    assert plan["authorities"]["source_activation"] is False
    assert tuple(tmp_path.rglob("*")) == before


def test_synthetic_publication_is_atomic_idempotent_and_non_active(
    tmp_path: Path,
) -> None:
    permit, _, _, plan = _synthetic_context(tmp_path)
    first = publish_alpaca_qualification_fixture(
        plan=plan,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    second = publish_alpaca_qualification_fixture(
        plan=plan,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    assert first.release_directory == second.release_directory
    assert first.release_id == second.release_id
    assert first.receipt_id == second.receipt_id
    assert first.release_directory == (
        tmp_path / "accepted" / publisher.FIXTURE_DATASET / first.release_id
    )
    receipt = verify_alpaca_qualification_release(
        first.release_directory,
        accepted_root=tmp_path / "accepted",
        expected_plan_id=plan["publication_plan_id"],
        synthetic=True,
    )
    assert receipt["status"] == SYNTHETIC_STATUS
    assert receipt["status"] != PRODUCTION_STATUS
    assert receipt["selected_feed"] == "sip"
    assert receipt["authorities"]["source_activation"] is False
    assert receipt["authorities"]["canonical_bars"] is False
    assert receipt["provenance"] == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"


def test_synthetic_publication_rejects_wrong_permit_clock_or_split_roots(
    tmp_path: Path,
) -> None:
    permit, _, _, plan = _synthetic_context(tmp_path)
    wrong = SyntheticOnlyPermit.create(
        fixture_id="wrong-alpaca-publication",
        scope=FIXTURE_SCOPE,
    )
    with pytest.raises(ContractError, match="differs from its permit"):
        publish_alpaca_qualification_fixture(
            plan=plan,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            clock=_clock(),
            permit=wrong,
        )
    with pytest.raises(PermissionError, match="synthetic clock"):
        publish_alpaca_qualification_fixture(
            plan=plan,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            clock=TrustedClock.production(),
            permit=permit,
        )
    with pytest.raises(ContractError, match="one fixture root"):
        publish_alpaca_qualification_fixture(
            plan=plan,
            accepted_root=tmp_path / "one" / "accepted",
            work_root=tmp_path / "two" / "work",
            clock=_clock(),
            permit=permit,
        )


def test_release_verification_rejects_receipt_mutation(tmp_path: Path) -> None:
    permit, _, _, plan = _synthetic_context(tmp_path)
    result = publish_alpaca_qualification_fixture(
        plan=plan,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    receipt_path = result.release_directory / publisher.PAYLOAD_FILENAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["authorities"]["source_activation"] = True
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(IntegrityError, match="payload hash mismatch"):
        verify_alpaca_qualification_release(
            result.release_directory,
            accepted_root=tmp_path / "accepted",
            synthetic=True,
        )


def test_production_publication_rejects_authority_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, plan = _synthetic_context(tmp_path)
    monkeypatch.setattr(
        publisher,
        "build_alpaca_qualification_publication_plan",
        lambda **kwargs: plan,
    )
    with pytest.raises(PermissionError, match="owner confirmation"):
        publish_alpaca_qualification_receipt(
            approved_plan_id=plan["publication_plan_id"],
            repo_root=REPO,
            accepted_root=tmp_path / "accepted-production",
            work_root=tmp_path / "work-production",
            clock=TrustedClock.production(),
            owner_confirmation="NO",
        )
    with pytest.raises(PermissionError, match="approved.*differs"):
        publish_alpaca_qualification_receipt(
            approved_plan_id="0" * 64,
            repo_root=REPO,
            accepted_root=tmp_path / "accepted-production",
            work_root=tmp_path / "work-production",
            clock=TrustedClock.production(),
            owner_confirmation="YES",
        )
    assert not (tmp_path / "accepted-production").exists()
    assert not (tmp_path / "work-production").exists()


def test_cli_defaults_to_no_write_plan_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = {
        "publication_plan_id": "a" * 64,
        "authorities": {"source_activation": False, "network_calls": False},
    }
    monkeypatch.setattr(
        publication_cli,
        "build_alpaca_qualification_publication_plan",
        lambda **kwargs: plan,
    )
    monkeypatch.setattr(
        publication_cli,
        "publish_alpaca_qualification_receipt",
        lambda **kwargs: pytest.fail("plan-only CLI attempted publication"),
    )
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))
    assert publication_main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "PLAN_ONLY_NO_WRITES"
    assert output["publication_authorized"] is False
    assert output["source_activation"] is False
    assert output["network_calls"] == 0
    assert tuple(tmp_path.rglob("*")) == before


def test_cli_execute_requires_confirmation_and_exact_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {"publication_plan_id": "a" * 64}
    monkeypatch.setattr(
        publication_cli,
        "build_alpaca_qualification_publication_plan",
        lambda **kwargs: plan,
    )
    monkeypatch.delenv(PUBLICATION_CONFIRMATION_TOKEN, raising=False)
    with pytest.raises(PermissionError, match=PUBLICATION_CONFIRMATION_TOKEN):
        publication_main(["--execute", "--approved-plan-id", "a" * 64])
    monkeypatch.setenv(PUBLICATION_CONFIRMATION_TOKEN, "YES")
    with pytest.raises(PermissionError, match="approved.*differs"):
        publication_main(["--execute", "--approved-plan-id", "b" * 64])


def test_publisher_has_no_network_or_secret_transport() -> None:
    source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "providers"
        / "alpaca_qualification_publisher.py"
    ).read_text(encoding="utf-8")
    cli_source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "cli"
        / "publish_alpaca_qualification.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "urllib",
        "open_without_redirects",
        "execute_network",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "api.env",
    ):
        assert forbidden not in source
        assert forbidden not in cli_source
