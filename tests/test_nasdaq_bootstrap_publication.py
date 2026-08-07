from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.cli.publish_nasdaq_bootstrap import main as publication_main
import us_stocks_swing_model_v2.cli.publish_nasdaq_bootstrap as publication_cli
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
import us_stocks_swing_model_v2.providers.nasdaq_bootstrap_publisher as publisher
from us_stocks_swing_model_v2.providers.nasdaq_bootstrap_publisher import (
    PRODUCTION_STATUS,
    PUBLICATION_CONFIRMATION_TOKEN,
    SYNTHETIC_STATUS,
    build_nasdaq_bootstrap_publication_plan,
    load_nasdaq_bootstrap_publication_policy,
    publish_nasdaq_bootstrap_fixture,
    publish_nasdaq_bootstrap_receipt,
    verify_nasdaq_bootstrap_baseline_release,
)
from us_stocks_swing_model_v2.providers.snapshots import NetworkAcquisitionRegistry


REPO = Path(__file__).resolve().parents[1]


def _publication_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="nasdaq-bootstrap-publication",
        scope="NASDAQ_BOOTSTRAP_PUBLICATION_FIXTURE",
    )


def _clock() -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        permit=SyntheticOnlyPermit.create(
            fixture_id="nasdaq-bootstrap-publication-clock",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _synthetic_context(tmp_path: Path):
    permit = _publication_permit()
    policy = {
        "plan_id": "1" * 64,
        "base_commit": "a" * 40,
        "snapshot_a": {
            "snapshot_id": "2" * 64,
            "raw_sha256": "3" * 64,
            "receipt_file_sha256": "4" * 64,
            "file_created_at": "2026-07-28T01:33:00Z",
            "retrieved_at": "2026-07-28T06:06:57Z",
            "record_count": 10,
        },
        "snapshot_b": {
            "snapshot_id": "5" * 64,
            "raw_sha256": "6" * 64,
            "receipt_file_sha256": "7" * 64,
            "file_created_at": "2026-07-28T11:01:00Z",
            "retrieved_at": "2026-07-28T11:16:14Z",
            "record_count": 11,
        },
        "receipt_contract": {"baseline_record_count": 11},
        "current_network_registry_id": "8" * 64,
        "current_environment_id": "9" * 64,
        "preserved_historical_receipt": {
            "path": "config/nasdaq_qualification_receipt.json",
            "file_sha256": "b" * 64,
            "receipt_id": "c" * 64,
            "role": "COMPARISON_ONLY_NOT_TRUSTED_NOT_GATE_INPUT",
        },
    }
    assessment = {
        "assessment_id": "d" * 64,
        "policy_id": "e" * 64,
    }
    plan = publisher._publication_plan_from_context(
        policy=policy,
        assessment=assessment,
        repository={"head": "f" * 40, "tree": "a" * 40},
        code_closure={"closure_sha256": "1" * 64},
        config_closure={"closure_sha256": "2" * 64},
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        synthetic_permit_id=permit.permit_id,
    )
    return permit, policy, plan


def test_checked_in_publication_policy_is_exact_and_non_authorizing() -> None:
    policy = load_nasdaq_bootstrap_publication_policy(REPO)
    payload = dict(policy)
    plan_id = payload.pop("plan_id")
    assert plan_id == (
        "48f5b555b8294b0777e02ea94610836f08f2e4b5333babe0b25e5b762995a6d6"
    )
    assert plan_id == sha256_bytes(canonical_json_bytes(payload))
    assert policy["execution_contract"]["default_mode"] == "PLAN_ONLY_NO_WRITES"
    assert policy["execution_contract"]["network_calls"] == 0
    assert policy["execution_contract"]["activation"] is False
    assert policy["destination"]["publication_count"] == 1
    assert policy["receipt_contract"]["source_active"] is False
    current_registry = NetworkAcquisitionRegistry.load(
        REPO / "config/network_acquisition_registry.json",
        allowed_root=REPO / "config",
    )
    assert policy["current_network_registry_id"] != current_registry.registry_id


def test_policy_shape_rejects_weakened_activation() -> None:
    policy = load_nasdaq_bootstrap_publication_policy(REPO)
    weakened = json.loads(json.dumps(policy))
    weakened["execution_contract"]["activation"] = True
    with pytest.raises(ContractError, match="execution boundary"):
        publisher._validate_publication_policy_shape(weakened)


def test_synthetic_publication_is_atomic_idempotent_and_not_trust_eligible(
    tmp_path: Path,
) -> None:
    permit, policy, plan = _synthetic_context(tmp_path)
    first = publish_nasdaq_bootstrap_fixture(
        plan=plan,
        policy=policy,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    second = publish_nasdaq_bootstrap_fixture(
        plan=plan,
        policy=policy,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    assert first.release_directory == second.release_directory
    assert first.release_id == second.release_id
    assert first.receipt_id == second.receipt_id
    assert first.release_directory == (
        tmp_path / "accepted" / "nasdaq_bootstrap_baseline" / first.release_id
    )
    receipt = verify_nasdaq_bootstrap_baseline_release(
        first.release_directory,
        accepted_root=tmp_path / "accepted",
        expected_plan_id=plan["publication_plan_id"],
        synthetic=True,
    )
    assert receipt["status"] == SYNTHETIC_STATUS
    assert receipt["status"] != PRODUCTION_STATUS
    assert receipt["baseline"]["continuity_baseline_eligible"] is False
    assert receipt["baseline"]["source_active"] is False
    assert receipt["authorities"]["source_activation"] is False
    assert receipt["provenance"] == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"


def test_synthetic_publication_rejects_wrong_permit_or_clock(tmp_path: Path) -> None:
    permit, policy, plan = _synthetic_context(tmp_path)
    wrong = SyntheticOnlyPermit.create(
        fixture_id="wrong-nasdaq-publication",
        scope="NASDAQ_BOOTSTRAP_PUBLICATION_FIXTURE",
    )
    with pytest.raises(ContractError, match="differs from its permit"):
        publish_nasdaq_bootstrap_fixture(
            plan=plan,
            policy=policy,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            clock=_clock(),
            permit=wrong,
        )
    with pytest.raises(PermissionError, match="synthetic clock"):
        publish_nasdaq_bootstrap_fixture(
            plan=plan,
            policy=policy,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            clock=TrustedClock.production(),
            permit=permit,
        )


def test_release_verification_rejects_receipt_mutation(tmp_path: Path) -> None:
    permit, policy, plan = _synthetic_context(tmp_path)
    result = publish_nasdaq_bootstrap_fixture(
        plan=plan,
        policy=policy,
        accepted_root=tmp_path / "accepted",
        work_root=tmp_path / "work",
        clock=_clock(),
        permit=permit,
    )
    receipt_path = result.release_directory / "nasdaq_bootstrap_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["baseline"]["source_active"] = True
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(IntegrityError, match="payload hash mismatch"):
        verify_nasdaq_bootstrap_baseline_release(
            result.release_directory,
            accepted_root=tmp_path / "accepted",
            synthetic=True,
        )


def test_plan_builder_binds_clean_successor_context_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_nasdaq_bootstrap_publication_policy(REPO)
    assessment = {
        "assessment_id": policy["assessment_id"],
        "policy_id": policy["bootstrap_policy_id"],
    }
    monkeypatch.setattr(
        publisher,
        "_repository_binding",
        lambda *args, **kwargs: {"head": "f" * 40, "tree": "e" * 40},
    )
    monkeypatch.setattr(
        publisher,
        "_validated_assessment",
        lambda *args, **kwargs: assessment,
    )
    monkeypatch.setattr(
        publisher,
        "_closure",
        lambda *args, **kwargs: {"files": [], "closure_sha256": "a" * 64},
    )
    plan = build_nasdaq_bootstrap_publication_plan(repo_root=REPO)
    plan_id = plan.pop("publication_plan_id")
    assert plan_id == sha256_bytes(canonical_json_bytes(plan))
    assert plan["publication_count"] == 1
    assert plan["authorities"]["source_activation"] is False
    assert plan["authorities"]["network_calls"] is False


def test_production_publication_rejects_unapproved_plan_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, plan = _synthetic_context(tmp_path)
    monkeypatch.setattr(
        publisher,
        "build_nasdaq_bootstrap_publication_plan",
        lambda **kwargs: plan,
    )
    monkeypatch.setattr(
        publisher,
        "load_nasdaq_bootstrap_publication_policy",
        lambda *args, **kwargs: {},
    )
    with pytest.raises(PermissionError, match="owner confirmation"):
        publish_nasdaq_bootstrap_receipt(
            approved_plan_id=plan["publication_plan_id"],
            repo_root=REPO,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            clock=TrustedClock.production(),
            owner_confirmation="NO",
        )
    with pytest.raises(PermissionError, match="approved.*differs"):
        publish_nasdaq_bootstrap_receipt(
            approved_plan_id="0" * 64,
            repo_root=REPO,
            accepted_root=tmp_path / "accepted",
            work_root=tmp_path / "work",
            clock=TrustedClock.production(),
            owner_confirmation="YES",
        )
    assert not (tmp_path / "accepted").exists()
    assert not (tmp_path / "work").exists()


def test_cli_defaults_to_plan_only_and_never_calls_publisher(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = {
        "publication_plan_id": "a" * 64,
        "authorities": {"source_activation": False, "network_calls": False},
    }
    monkeypatch.setattr(
        publication_cli,
        "build_nasdaq_bootstrap_publication_plan",
        lambda **kwargs: plan,
    )
    monkeypatch.setattr(
        publication_cli,
        "publish_nasdaq_bootstrap_receipt",
        lambda **kwargs: pytest.fail("plan-only CLI attempted publication"),
    )
    assert publication_main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "PLAN_ONLY_NO_WRITES"
    assert output["publication_authorized"] is False
    assert output["source_activation"] is False
    assert output["network_calls"] == 0


def test_cli_execute_requires_confirmation_and_exact_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {"publication_plan_id": "a" * 64}
    monkeypatch.setattr(
        publication_cli,
        "build_nasdaq_bootstrap_publication_plan",
        lambda **kwargs: plan,
    )
    monkeypatch.delenv(PUBLICATION_CONFIRMATION_TOKEN, raising=False)
    with pytest.raises(PermissionError, match=PUBLICATION_CONFIRMATION_TOKEN):
        publication_main(
            ["--execute", "--approved-plan-id", "a" * 64]
        )
    monkeypatch.setenv(PUBLICATION_CONFIRMATION_TOKEN, "YES")
    with pytest.raises(PermissionError, match="approved.*differs"):
        publication_main(
            ["--execute", "--approved-plan-id", "b" * 64]
        )


def test_publisher_has_no_network_transport_imports() -> None:
    source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "providers"
        / "nasdaq_bootstrap_publisher.py"
    ).read_text(encoding="utf-8")
    assert "urllib" not in source
    assert "open_without_redirects" not in source
    assert "execute_network" not in source
