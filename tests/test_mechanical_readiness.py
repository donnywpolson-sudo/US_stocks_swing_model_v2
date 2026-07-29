from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
from typing import Iterator

import pytest

import test_foundation_orchestrator as foundation_support
import us_stocks_swing_model_v2.historical_foundation as foundation_module
import us_stocks_swing_model_v2.mechanical_readiness as readiness_module
from us_stocks_swing_model_v2.bundle import BLOCKED_READINESS_RECEIPT_ID
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import IntegrityError
from us_stocks_swing_model_v2.mechanical_readiness import (
    HISTORICAL_READY_DATASET,
    HISTORICAL_READY_MILESTONE,
    MECHANICAL_READINESS_PUBLICATION_AUTHORIZATION_SCOPE,
    READINESS_STATE,
    REBUILD_DATASET,
    REBUILD_MILESTONE,
    _role_isolation_violations,
    assess_stock_mechanical_readiness,
    build_mechanical_isolation_attestation,
    mechanical_readiness_authorization_bindings,
    publish_stock_mechanical_readiness,
    verify_mechanical_isolation_attestation,
    verify_stock_mechanical_readiness_publication,
)
from us_stocks_swing_model_v2.releases import (
    AtomicReleasePublisher,
    build_manifest,
    verify_accepted_release,
)


CREATED_AT = "2026-07-15T12:30:00Z"


@pytest.fixture
def readiness_tmp() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="smr-"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _foundation(root: Path):
    _, permit, _, accepted, _, result = foundation_support._run(root)
    return permit, accepted, result.aggregate_set_release_directory


def _receipts(result):
    rebuild = json.loads(
        (
            result.rebuild_complete_release_directory / "rebuild_complete.json"
        ).read_text(encoding="utf-8")
    )
    historical = json.loads(
        (
            result.historical_research_ready_release_directory
            / "historical_research_ready.json"
        ).read_text(encoding="utf-8")
    )
    return rebuild, historical


def test_assessment_and_two_receipts_are_mechanical_only_and_idempotent(
    readiness_tmp: Path,
) -> None:
    permit, accepted, foundation = _foundation(readiness_tmp)
    assessment = assess_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        synthetic_permit=permit,
    )
    value = assessment.as_dict()
    assert value["readiness_state"] == READINESS_STATE
    assert value["foundation"]["component_count"] == 11
    assert len(set(value["foundation"]["component_release_ids"])) == 11
    assert value["foundation"]["point_in_time_safe"] is False
    assert value["legacy_exposure"]["documented_lower_bound"] >= 62
    assert value["legacy_exposure"]["exact_count"] is None
    assert value["legacy_exposure"]["exact_count_state"] == "INDETERMINATE"
    assert value["legacy_exposure"]["fabricated_trial_events"] is False
    assert value["authority_and_claims"] == readiness_module._AUTHORITY_AND_CLAIMS

    result = publish_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        readiness_work_root=readiness_tmp / "ready-work",
        created_at=CREATED_AT,
        synthetic_permit=permit,
    )
    rebuild_manifest = verify_accepted_release(
        result.rebuild_complete_release_directory, accepted_root=accepted
    )
    historical_manifest = verify_accepted_release(
        result.historical_research_ready_release_directory, accepted_root=accepted
    )
    assert rebuild_manifest.dataset == REBUILD_DATASET
    assert historical_manifest.dataset == HISTORICAL_READY_DATASET
    assert result.rebuild_complete_release_directory != (
        result.historical_research_ready_release_directory
    )
    rebuild, historical = _receipts(result)
    assert rebuild["milestone"] == REBUILD_MILESTONE
    assert historical["milestone"] == HISTORICAL_READY_MILESTONE
    assert historical["upstream_milestone"]["release_id"] == rebuild_manifest.release_id
    assert rebuild["readiness_state"] == historical["readiness_state"] == READINESS_STATE
    assert rebuild["receipt_id"] != BLOCKED_READINESS_RECEIPT_ID
    for receipt in (rebuild, historical):
        assert receipt["schema_version"] == 2
        assert receipt["local_integrity_record"] is None
        assert receipt["authority_and_claims"] == readiness_module._AUTHORITY_AND_CLAIMS
        assert receipt["pit_guard"]["historical_pit_identity_evidence"] == (
            "UNRESOLVED_NOT_FABRICATED"
        )
        assert receipt["pit_guard"]["genuinely_prospective_pit_evidence_required"] is True
        assert receipt["pit_guard"]["candidate_eligibility"] == (
            "BLOCKED_PENDING_GENUINELY_PROSPECTIVE_PIT"
        )
    repeated = publish_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        readiness_work_root=readiness_tmp / "ready-work",
        created_at=CREATED_AT,
        synthetic_permit=permit,
    )
    assert repeated == result


def test_publication_reuses_the_verified_assessment_once(
    readiness_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit, accepted, foundation = _foundation(readiness_tmp)
    original = readiness_module.assess_stock_mechanical_readiness
    calls = 0

    def counted_assessment(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(
        readiness_module, "assess_stock_mechanical_readiness", counted_assessment
    )
    publish_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        readiness_work_root=readiness_tmp / "ready-work-single-assessment",
        created_at=CREATED_AT,
        synthetic_permit=permit,
    )
    assert calls == 1


def test_assessment_verifies_hfdl_once(
    readiness_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit, accepted, foundation = _foundation(readiness_tmp)
    original = foundation_module.verify_hfdl_legacy_publication
    calls = 0

    def counted_hfdl_verification(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        foundation_module,
        "verify_hfdl_legacy_publication",
        counted_hfdl_verification,
    )
    assess_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        synthetic_permit=permit,
    )
    assert calls == 1


def test_production_publication_requires_production_clock_before_mutation(
    readiness_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit, accepted, foundation = _foundation(readiness_tmp)
    assessment = assess_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        synthetic_permit=permit,
    )
    data_root = readiness_tmp / "data"
    data_root.mkdir(exist_ok=True)
    work_root = (data_root / "readiness" / "authorized-output").resolve(strict=False)
    monkeypatch.setattr(readiness_module, "_repo_root", lambda: readiness_tmp)
    monkeypatch.setattr(
        readiness_module,
        "assess_stock_mechanical_readiness",
        lambda **_kwargs: assessment,
    )

    bindings = mechanical_readiness_authorization_bindings(
        assessment=assessment,
        accepted_release_root=accepted,
        readiness_work_root=work_root,
        created_at=CREATED_AT,
    )
    assert MECHANICAL_READINESS_PUBLICATION_AUTHORIZATION_SCOPE == (
        "AUTHORIZE_MECHANICAL_READINESS_PUBLICATION"
    )
    assert bindings == {
        "accepted_release_root": str(accepted.resolve(strict=True)),
        "assessment_id": assessment.assessment_id,
        "created_at": CREATED_AT,
        "foundation_release_id": assessment.foundation["release_id"],
        "historical_ready_dataset": HISTORICAL_READY_DATASET,
        "historical_ready_filename": "historical_research_ready.json",
        "project": "US_stocks_swing_model_v2",
        "publication_count": "2",
        "readiness_work_root": str(work_root.resolve(strict=False)),
        "rebuild_dataset": REBUILD_DATASET,
        "rebuild_filename": "rebuild_complete.json",
    }
    with pytest.raises(PermissionError, match="production system UTC clock"):
        publish_stock_mechanical_readiness(
            foundation_release_directory=foundation,
            accepted_release_root=accepted,
            readiness_work_root=work_root,
            created_at=CREATED_AT,
        )
    assert not work_root.exists()

    result = publish_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        readiness_work_root=work_root,
        created_at=CREATED_AT,
        clock=TrustedClock.production(),
    )
    assert result.local_action_record is not None
    record = result.local_action_record
    assert record.scope == MECHANICAL_READINESS_PUBLICATION_AUTHORIZATION_SCOPE
    assert record.subject_id == assessment.assessment_id
    assert dict(record.bindings) == bindings
    rebuild, historical = _receipts(result)
    assert rebuild["schema_version"] == historical["schema_version"] == 2
    assert rebuild["local_integrity_record"] == historical["local_integrity_record"]
    assert rebuild["local_integrity_record"] == record.as_dict()

    verified = verify_stock_mechanical_readiness_publication(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        rebuild_complete_release_directory=(
            result.rebuild_complete_release_directory
        ),
        historical_research_ready_release_directory=(
            result.historical_research_ready_release_directory
        ),
    )
    assert verified.local_action_record == record

    repeated = publish_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        readiness_work_root=work_root,
        created_at=CREATED_AT,
        clock=TrustedClock.production(),
    )
    assert repeated == result


def test_production_readiness_legacy_record_omission_and_mismatch_fail_closed(
    readiness_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permit, accepted, foundation = _foundation(readiness_tmp)
    assessment = assess_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        synthetic_permit=permit,
    )
    data_root = readiness_tmp / "data"
    data_root.mkdir(exist_ok=True)
    work_root = (data_root / "readiness" / "bound-output").resolve(strict=False)
    monkeypatch.setattr(readiness_module, "_repo_root", lambda: readiness_tmp)
    monkeypatch.setattr(
        readiness_module,
        "assess_stock_mechanical_readiness",
        lambda **_kwargs: assessment,
    )
    result = publish_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        readiness_work_root=work_root,
        created_at=CREATED_AT,
        clock=TrustedClock.production(),
    )
    receipt_path = (
        result.rebuild_complete_release_directory / "rebuild_complete.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["schema_version"] = 1
    receipt.pop("local_integrity_record")
    unsigned_receipt = dict(receipt)
    unsigned_receipt.pop("receipt_id")
    receipt["receipt_id"] = sha256_bytes(canonical_json_bytes(unsigned_receipt))
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(IntegrityError, match="lacks its local action record"):
        verify_stock_mechanical_readiness_publication(
            foundation_release_directory=foundation,
            accepted_release_root=accepted,
            rebuild_complete_release_directory=(
                result.rebuild_complete_release_directory
            ),
            historical_research_ready_release_directory=(
                result.historical_research_ready_release_directory
            ),
        )

    record_path = next(work_root.rglob("local_action_record.json"))
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    persisted["scope"] = "WRONG_LOCAL_SCOPE"
    unsigned_record = dict(persisted)
    unsigned_record.pop("record_id")
    persisted["record_id"] = sha256_bytes(canonical_json_bytes(unsigned_record))
    record_path.write_bytes(canonical_json_bytes(persisted))
    with pytest.raises(IntegrityError, match="local action record differs"):
        publish_stock_mechanical_readiness(
            foundation_release_directory=foundation,
            accepted_release_root=accepted,
            readiness_work_root=work_root,
            created_at=CREATED_AT,
            clock=TrustedClock.production(),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["trial_ledger"]["legacy_trial_census"].__setitem__(
                "documented_minimum_outcome_informed_attempts", 61
            ),
            "conservative indeterminate floor",
        ),
        (
            lambda value: value["trial_ledger"]["legacy_trial_census"].__setitem__(
                "documented_minimum_outcome_informed_attempts", True
            ),
            "conservative indeterminate floor",
        ),
        (
            lambda value: value["trial_ledger"]["legacy_trial_census"].__setitem__(
                "exact_count_state", "EXACT"
            ),
            "conservative indeterminate floor",
        ),
        (
            lambda value: value["trial_ledger"]["legacy_trial_census"].__setitem__(
                "exact_count", 62
            ),
            "conservative indeterminate floor",
        ),
        (
            lambda value: value["readiness"].__setitem__(
                "pit_blockers", value["readiness"]["pit_blockers"][1:]
            ),
            "blockers or isolation",
        ),
        (
            lambda value: value["readiness"].__setitem__(
                "candidate_eligibility", "ELIGIBLE"
            ),
            "blockers or isolation",
        ),
        (
            lambda value: value["readiness"].__setitem__("ready", True),
            "blockers or isolation",
        ),
    ],
)
def test_census_and_pit_blocker_substitutions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    message: str,
) -> None:
    contract_path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "research_readiness_contract.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    mutation(contract)
    original = readiness_module._json_object

    def substituted(path: Path, *, canonical: bool, label: str):
        if Path(path).name == "research_readiness_contract.json":
            return deepcopy(contract)
        return original(path, canonical=canonical, label=label)

    monkeypatch.setattr(readiness_module, "_json_object", substituted)
    with pytest.raises(IntegrityError, match=message):
        readiness_module._load_registered_contract()


def test_isolation_attestation_tamper_and_recomputed_id_still_fail() -> None:
    attestation = build_mechanical_isolation_attestation()
    assert attestation["analysis_policy_version"] == (
        "FAIL_CLOSED_ROLE_ISOLATION_AST_V2"
    )
    assert attestation["analysis_boundary"] == (
        "STATIC_SOURCE_AST_ONLY_NOT_RUNTIME_PROOF"
    )
    tampered = dict(attestation)
    tampered["model_fit_executed"] = True
    unsigned = dict(tampered)
    unsigned.pop("attestation_id")
    tampered["attestation_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    with pytest.raises(IntegrityError, match="differs from current source"):
        verify_mechanical_isolation_attestation(tampered)


@pytest.mark.parametrize(
    "value",
    [
        "futures_intraday_model_v2/data",
        "futures_rebuild\\artifacts",
        "C:/foreign/futures_intraday_model/data",
    ],
)
def test_mechanical_isolation_detects_foreign_project_path_components(
    value: str,
) -> None:
    assert readiness_module._contains_foreign_project_path_literal(value)


@pytest.mark.parametrize(
    "value",
    [
        "futures_intraday_model_v2",
        "futures_intraday_model_v20/data",
        "my_futures_rebuild/artifacts",
        "C:/foreign/futures_intraday_model_notes/data",
    ],
)
def test_mechanical_isolation_ignores_prefix_like_path_components(
    value: str,
) -> None:
    assert not readiness_module._contains_foreign_project_path_literal(value)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("model.fit(values)", "fit_reference"),
        ("runner = model.fit\nrunner(values)", "fit_reference"),
        ("getattr(model, 'fit')(values)", "dynamic_reference:getattr"),
        ("__import__('sklearn')", "dynamic_reference:__import__"),
        (
            "import importlib\nimportlib.import_module('sklearn')",
            "dynamic_import_reference",
        ),
        (
            "from importlib import import_module as load\nload('sklearn')",
            "dynamic_import_alias:import_module",
        ),
        (
            "from builtins import eval as evaluate\n"
            "evaluate('1 + 1')",
            "dynamic_import_alias_reference:eval",
        ),
        (
            "from builtins import exec as run\n"
            "run('value = 1')",
            "dynamic_import_alias_reference:exec",
        ),
        (
            "from builtins import __import__ as load\n"
            "load('sklearn')",
            "dynamic_import_alias_reference:__import__",
        ),
        (
            "from importlib import import_module as load\n"
            "load('sklearn')",
            "dynamic_import_alias_reference:import_module",
        ),
        ("eval('model.' + 'fit(values)')", "dynamic_reference:eval"),
        ("globals()['__import__']('sklearn')", "dynamic_reference:globals"),
        (
            "__builtins__['__import__']('sklearn')",
            "dynamic_reference:__builtins__",
        ),
        (
            "import builtins\nbuiltins.__import__('sklearn')",
            "dynamic_attribute_reference:__import__",
        ),
        (
            "import operator\noperator.attrgetter('fit')(model)(values)",
            "dynamic_attribute_reference:attrgetter",
        ),
        (
            "model.__dict__['fit'](values)",
            "dynamic_attribute_reference:__dict__",
        ),
        (
            "setattr(model, 'runner', model.fit)",
            "dynamic_reference:setattr",
        ),
    ],
)
def test_role_isolation_ast_rejects_indirect_execution(
    source: str,
    expected: str,
) -> None:
    violations = _role_isolation_violations(ast.parse(source))
    assert any(item.startswith(expected) for item in violations)


def test_role_isolation_ast_accepts_plain_fit_free_scoring() -> None:
    tree = ast.parse(
        "def score(values):\n"
        "    return sum(value * 2.0 for value in values)\n"
    )
    assert _role_isolation_violations(tree) == ()


def test_role_isolation_ast_accepts_unrelated_import_alias() -> None:
    tree = ast.parse("from math import sqrt as root\nvalue = root(4.0)\n")
    assert _role_isolation_violations(tree) == ()


def _substituted_aggregate(
    root: Path,
    *,
    foundation: Path,
    accepted: Path,
) -> Path:
    original_manifest = verify_accepted_release(foundation, accepted_root=accepted)
    receipt = json.loads((foundation / "foundation_set.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (foundation / "foundation_index.jsonl").read_bytes().splitlines()
    ]
    source = rows[2]
    target = rows[1]
    for field in (
        "dataset",
        "release_id",
        "relative_directory",
        "source_epoch",
        "role",
        "quality_state",
        "row_count",
        "event_start",
        "event_end",
        "manifest_sha256",
    ):
        target[field] = source[field]
    receipt["hfdl"]["epochs"][target["epoch"]] = {
        key: target[key] for key in target if key != "sequence"
    }
    index_bytes = b"".join(canonical_json_bytes(row) for row in rows)
    receipt["index_sha256"] = sha256_bytes(index_bytes)
    receipt["component_release_ids"] = sorted(row["release_id"] for row in rows)
    stage = root / "substituted-stage"
    stage.mkdir()
    (stage / "foundation_index.jsonl").write_bytes(index_bytes)
    (stage / "foundation_set.json").write_bytes(canonical_json_bytes(receipt))
    event_starts = [row["event_start"] for row in rows if row["event_start"] is not None]
    event_ends = [row["event_end"] for row in rows if row["event_end"] is not None]
    manifest = build_manifest(
        stage,
        ("foundation_index.jsonl", "foundation_set.json"),
        project=original_manifest.project,
        dataset=original_manifest.dataset,
        source_epoch=original_manifest.source_epoch,
        role=original_manifest.role,
        quality_state=original_manifest.quality_state,
        created_at=original_manifest.created_at,
        row_count=11,
        event_start=min(event_starts),
        event_end=max(event_ends),
        upstream_release_ids=(row["release_id"] for row in rows),
        schema_fingerprint=original_manifest.schema_fingerprint,
        code_hash=original_manifest.code_hash,
        config_hash=original_manifest.config_hash,
        environment_hash=original_manifest.environment_hash,
    )
    return AtomicReleasePublisher(accepted).publish(stage, manifest)


def test_self_consistent_component_substitution_is_not_readiness(
    readiness_tmp: Path,
) -> None:
    permit, accepted, foundation = _foundation(readiness_tmp)
    substituted = _substituted_aggregate(
        readiness_tmp, foundation=foundation, accepted=accepted
    )
    verify_accepted_release(substituted, accepted_root=accepted)
    with pytest.raises(IntegrityError, match="component identity differs"):
        assess_stock_mechanical_readiness(
            foundation_release_directory=substituted,
            accepted_release_root=accepted,
            synthetic_permit=permit,
        )


def test_receipt_tamper_and_cross_kind_substitution_fail_closed(
    readiness_tmp: Path,
) -> None:
    permit, accepted, foundation = _foundation(readiness_tmp)
    result = publish_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted,
        readiness_work_root=readiness_tmp / "work",
        created_at=CREATED_AT,
        synthetic_permit=permit,
    )
    with pytest.raises(IntegrityError):
        verify_stock_mechanical_readiness_publication(
            foundation_release_directory=foundation,
            accepted_release_root=accepted,
            rebuild_complete_release_directory=(
                result.historical_research_ready_release_directory
            ),
            historical_research_ready_release_directory=(
                result.rebuild_complete_release_directory
            ),
            synthetic_permit=permit,
        )
    receipt_path = (
        result.historical_research_ready_release_directory
        / "historical_research_ready.json"
    )
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    value["authority_and_claims"]["candidate_eligible"] = True
    unsigned = dict(value)
    unsigned.pop("receipt_id")
    value["receipt_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    receipt_path.write_bytes(canonical_json_bytes(value))
    with pytest.raises(IntegrityError):
        verify_stock_mechanical_readiness_publication(
            foundation_release_directory=foundation,
            accepted_release_root=accepted,
            rebuild_complete_release_directory=result.rebuild_complete_release_directory,
            historical_research_ready_release_directory=(
                result.historical_research_ready_release_directory
            ),
            synthetic_permit=permit,
        )


def test_missing_or_wrong_foundation_and_dirty_production_closure_block(
    readiness_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permit, accepted, foundation = _foundation(readiness_tmp)
    receipt = json.loads((foundation / "foundation_set.json").read_text(encoding="utf-8"))
    calendar_binding = receipt["calendar"]["release"]
    calendar = accepted / calendar_binding["dataset"] / calendar_binding["release_id"]
    with pytest.raises(IntegrityError, match="foundation aggregate contract differs"):
        assess_stock_mechanical_readiness(
            foundation_release_directory=calendar,
            accepted_release_root=accepted,
            synthetic_permit=permit,
        )

    def dirty_git(*arguments: str) -> str:
        if arguments[:1] == ("rev-parse",):
            if arguments[-1] == "--show-toplevel":
                return str(Path(__file__).resolve().parents[1])
            if arguments[-1] == "--absolute-git-dir":
                return str(Path(__file__).resolve().parents[1] / ".git")
        if arguments[:1] == ("status",):
            return "?? untracked.py"
        return "0" * 64

    monkeypatch.setattr(readiness_module, "_run_git", dirty_git)
    with pytest.raises(IntegrityError, match="clean tracked repository"):
        readiness_module._repository_binding(None)


def test_cli_and_issuer_have_no_execution_backdoor() -> None:
    root = Path(__file__).resolve().parents[1]
    cli = (
        root
        / "src"
        / "us_stocks_swing_model_v2"
        / "cli"
        / "assess_mechanical_readiness.py"
    )
    issuer = (
        root
        / "src"
        / "us_stocks_swing_model_v2"
        / "mechanical_readiness.py"
    )
    cli_text = cli.read_text(encoding="utf-8")
    for switch in (
        "--provider",
        "--download",
        "--model",
        "--fit",
        "--wfa",
        "--label",
        "--candidate",
        "--trade",
    ):
        assert switch not in cli_text
    tree = ast.parse(issuer.read_text(encoding="utf-8"))
    executed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "execute_synthetic_nested_wfa")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute_synthetic_nested_wfa"
            )
        )
    ]
    assert executed == []
    bundle_source = (
        root / "src" / "us_stocks_swing_model_v2" / "bundle.py"
    ).read_text(encoding="utf-8")
    assert "self.readiness_receipt_id != BLOCKED_READINESS_RECEIPT_ID" in bundle_source
