from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.audit_controls import (
    AUDIT_SURFACES,
    ProspectiveControlProtocol,
    ProviderLineageEvidence,
    TraceabilityRow,
    assess_traceability_matrix,
    authorize_aggregate_read,
    require_direct_final_holdout_query,
    require_next_attempt,
    scan_declared_audit_surfaces,
    verify_prospective_vintage_census,
)
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.errors import (
    ContractError,
    EvaluationAuthorizationError,
    IntegrityError,
)
from us_stocks_swing_model_v2 import meta_audit_harness
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.research.contracts import ResearchContractError
from us_stocks_swing_model_v2.research.economics import (
    DailyCohortBook,
    EconomicPolicy,
    reconstruct_five_cohort_economics,
)
from us_stocks_swing_model_v2.trials import build_holdout_receipt


def _meta_binding(root: Path, relative_path: str) -> meta_audit_harness.FileBinding:
    payload = (root / relative_path).read_bytes()
    return meta_audit_harness.FileBinding(
        path=relative_path,
        bytes=len(payload),
        sha256=sha256_bytes(payload),
        git_blob=meta_audit_harness.git_blob_sha1_bytes(payload),
    )


def _meta_v2_envelope(root: Path) -> dict[str, object]:
    payloads = {
        "reference.txt": b"reference one\nreference two\n",
        "target.txt": b"target one\ntarget two\n",
        "controller.md": b"controller\n",
        "corpus.json": b"{}\n",
        "reader.ps1": b"# reader\n",
    }
    for relative_path, payload in payloads.items():
        (root / relative_path).write_bytes(payload)
    reference = _meta_binding(root, "reference.txt")
    target = _meta_binding(root, "target.txt")
    controller = _meta_binding(root, "controller.md")
    corpus = _meta_binding(root, "corpus.json")
    script = _meta_binding(root, "reader.ps1")
    groups = meta_audit_harness.build_maximal_read_groups(
        root=root,
        reference_bindings=(reference,),
        target_binding=target,
    )
    powershell = root / "pwsh.exe"
    commands = [
        meta_audit_harness._v2_command(
            ordinal=1,
            mode="Preflight",
            group_ordinal=None,
            root=root,
            powershell_executable=powershell,
            script_path=root / script.path,
            timeout_seconds=30,
            output_max_utf8_bytes=4_000,
        ),
        meta_audit_harness._v2_command(
            ordinal=2,
            mode="PlanGroups",
            group_ordinal=None,
            root=root,
            powershell_executable=powershell,
            script_path=root / script.path,
            timeout_seconds=30,
            output_max_utf8_bytes=4_000,
        ),
    ]
    for group in groups:
        commands.append(
            meta_audit_harness._v2_command(
                ordinal=len(commands) + 1,
                mode="ReadGroup",
                group_ordinal=group.group_ordinal,
                root=root,
                powershell_executable=powershell,
                script_path=root / script.path,
                timeout_seconds=60,
                output_max_utf8_bytes=group.rendered_utf8_bytes,
            )
        )
    commands.append(
        meta_audit_harness._v2_command(
            ordinal=len(commands) + 1,
            mode="FinalPreflight",
            group_ordinal=None,
            root=root,
            powershell_executable=powershell,
            script_path=root / script.path,
            timeout_seconds=30,
            output_max_utf8_bytes=4_000,
        )
    )
    first_target_group = next(
        group.group_ordinal for group in groups if group.phase == "TARGET"
    )
    unsigned = {
        "schema_version": 2,
        "repository": {
            "root": str(root),
            "branch": "main",
            "head": "a" * 40,
            "tree": "b" * 40,
            "require_clean": True,
        },
        "host": {
            "powershell_executable": str(powershell),
            "powershell_sha256": "c" * 64,
            "powershell_file_version": "1",
            "ps_version": "7",
            "ps_edition": "Core",
            "clr_version": "1",
            "is_64bit_process": True,
            "sha256_hash_data_available": True,
            "sha1_hash_data_available": True,
            "path_get_relative_path_available": True,
        },
        "script": script.as_dict(),
        "target": target.as_dict(),
        "controller": controller.as_dict(),
        "corpus_policy": corpus.as_dict(),
        "reference_census": {
            "count": 1,
            "sha256": sha256_bytes(canonical_json_bytes([reference.as_dict()])),
            "paths_sha256": sha256_bytes(canonical_json_bytes([reference.path])),
        },
        "read_groups": [group.as_dict() for group in groups],
        "commands": commands,
        "barriers": [
            {
                "name": "B01_BLIND_CENSUS_FROZEN",
                "after_command_ordinal": first_target_group + 1,
                "before_command_ordinal": first_target_group + 2,
            },
            {
                "name": "B02_MAPPING_COMPLETE",
                "after_command_ordinal": len(groups) + 2,
                "before_command_ordinal": len(groups) + 3,
            },
        ],
        "reviewer_independence": {
            "reviewer_instance_binding": "d" * 64,
            "no_inherited_turns": True,
            "no_prior_target_access": True,
            "target_access_barrier": "B01_BLIND_CENSUS_FROZEN",
            "final_attestation_required": True,
        },
        "failure_class": "READ_ONLY_INVOCATION",
        "encoding": {"name": "UTF-8", "bom": False, "console": "UTF-8"},
        "output": {"destination": "CONVERSATION_ONLY", "retained": False},
        "prohibitions": list(meta_audit_harness.PROHIBITIONS),
    }
    return meta_audit_harness.build_v2_envelope_payload(unsigned)


def _surface_census(
    root: Path,
    *,
    poisoned_surface: str | None = None,
    forbidden_filename: bool = False,
) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for surface in AUDIT_SURFACES:
        directory = root / surface
        directory.mkdir(parents=True)
        filename = "api.env" if forbidden_filename and surface == poisoned_surface else "evidence.txt"
        path = directory / filename
        payload = (
            b"Authorization: Bearer secret-token-value"
            if surface == poisoned_surface and not forbidden_filename
            else b"ordinary audit evidence"
        )
        path.write_bytes(payload)
        result[surface] = (path.relative_to(root).as_posix(),)
    return result


def _surface_roots() -> dict[str, tuple[str, ...]]:
    return {surface: (surface,) for surface in AUDIT_SURFACES}


def test_secret_scan_covers_every_surface_and_reports_no_secret_bytes(tmp_path: Path) -> None:
    clean = scan_declared_audit_surfaces(
        tmp_path,
        _surface_census(tmp_path),
        surface_roots=_surface_roots(),
    )
    assert clean.passed is True
    assert clean.findings == ()
    assert tuple(name for name, _ in clean.surface_counts) == AUDIT_SURFACES

    poison_root = tmp_path / "poison"
    poisoned = scan_declared_audit_surfaces(
        poison_root,
        _surface_census(poison_root, poisoned_surface="reports"),
        surface_roots=_surface_roots(),
    )
    assert poisoned.passed is False
    assert len(poisoned.findings) == 1
    finding = poisoned.findings[0]
    assert finding.surface == "reports"
    assert finding.category == "BEARER_TOKEN"
    assert finding.line_number == 1
    assert "secret-token-value" not in repr(finding)


@pytest.mark.parametrize("surface", AUDIT_SURFACES)
def test_secret_scan_detects_each_required_surface(surface: str, tmp_path: Path) -> None:
    result = scan_declared_audit_surfaces(
        tmp_path,
        _surface_census(tmp_path, poisoned_surface=surface),
        surface_roots=_surface_roots(),
    )
    assert result.passed is False
    assert {finding.surface for finding in result.findings} == {surface}


def test_secret_filename_is_flagged_without_reading_its_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    surfaces = _surface_census(
        tmp_path,
        poisoned_surface="admitted_evidence",
        forbidden_filename=True,
    )
    original = Path.read_bytes

    def guarded_read(path: Path) -> bytes:
        if path.name == "api.env":
            raise AssertionError("forbidden secret source bytes were read")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    result = scan_declared_audit_surfaces(
        tmp_path,
        surfaces,
        surface_roots=_surface_roots(),
    )
    assert result.passed is False
    assert result.findings[0].category == "FORBIDDEN_SECRET_FILENAME"
    assert result.findings[0].line_number is None
    assert result.findings[0].file_sha256 is None


def test_secret_scan_proves_absent_and_empty_surfaces_and_rejects_unexpected_files(
    tmp_path: Path,
) -> None:
    surfaces = _surface_census(tmp_path)
    (tmp_path / surfaces["logs"][0]).unlink()
    (tmp_path / "logs").rmdir()
    (tmp_path / surfaces["artifacts"][0]).unlink()
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    surfaces["logs"] = ()
    surfaces["artifacts"] = ()
    result = scan_declared_audit_surfaces(
        tmp_path,
        surfaces,
        surface_roots=_surface_roots(),
    )
    assert result.passed is True
    assert dict(result.surface_counts)["logs"] == 0
    assert dict(result.surface_counts)["artifacts"] == 0
    states = {
        (root.surface, root.relative_path): root.state
        for root in result.surface_roots
    }
    assert states[("logs", "logs")] == "ABSENT"
    assert states[("artifacts", "artifacts")] == "EMPTY_DIRECTORY"

    (tmp_path / "artifacts" / "unexpected.txt").write_text(
        "unexpected",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="differs from files below its roots"):
        scan_declared_audit_surfaces(
            tmp_path,
            surfaces,
            surface_roots=_surface_roots(),
        )


def test_secret_scan_rejects_a_file_omitted_from_the_declared_census(tmp_path: Path) -> None:
    surfaces = _surface_census(tmp_path)
    surfaces["logs"] = ()
    with pytest.raises(ContractError, match="differs from files below its roots"):
        scan_declared_audit_surfaces(
            tmp_path,
            surfaces,
            surface_roots=_surface_roots(),
        )


def _traceability_row(
    requirement_id: str,
    threat_id: str,
    *,
    severity: str = "High",
    priority: str = "P1",
    mutation_result: str = "FAIL_CLOSED",
    disposition: str = "CLOSED",
    evidence_id: str | None = "a" * 64,
) -> TraceabilityRow:
    return TraceabilityRow(
        requirement_id=requirement_id,
        threat_id=threat_id,
        severity=severity,
        priority=priority,
        authoritative_owner="constitution",
        master_control="G1",
        gate="G1",
        enforcement="audit_controls",
        test_evidence="test_meta_audit_remediation",
        mutation_result=mutation_result,
        evidence_id=evidence_id,
        residual_risk="none",
        disposition=disposition,
        remediation_owner="audit_owner",
    )


def test_traceability_requires_exact_requirement_and_threat_closure() -> None:
    rows = (
        _traceability_row("B01", "T01", severity="Critical", priority="P0"),
        _traceability_row("B02", "T02", severity="Low", priority="P3"),
    )
    result = assess_traceability_matrix(("B01", "B02"), ("T01", "T02"), rows)
    assert result.classification == "SUPPORTABLE"
    assert result.unresolved_critical_high == ()

    with pytest.raises(ContractError, match="exactly cover requirements"):
        assess_traceability_matrix(("B01", "B02", "B03"), ("T01", "T02"), rows)
    with pytest.raises(ContractError, match="exactly cover threats"):
        assess_traceability_matrix(("B01", "B02"), ("T01", "T02", "T03"), rows)


def test_traceability_false_pass_and_open_high_findings_cannot_support() -> None:
    blocked = assess_traceability_matrix(
        ("B01",),
        ("T01",),
        (
            _traceability_row(
                "B01",
                "T01",
                mutation_result="FALSE_PASS",
                disposition="OPEN",
                evidence_id=None,
            ),
        ),
    )
    assert blocked.classification == "BLOCKED"

    insufficient = assess_traceability_matrix(
        ("B01",),
        ("T01",),
        (
            _traceability_row(
                "B01",
                "T01",
                mutation_result="NOT_RUN",
                disposition="MISSING_EVIDENCE",
                evidence_id=None,
            ),
        ),
    )
    assert insufficient.classification == "INSUFFICIENT_EVIDENCE"
    assert insufficient.unresolved_critical_high == ("B01",)


def test_provider_lineage_binds_raw_headers_request_and_pagination() -> None:
    pages = (b"provider-page-one", b"provider-page-two")
    evidence = ProviderLineageEvidence.create(
        raw_bytes=b"".join(pages),
        page_payloads=pages,
        expected_page_count=2,
        response_headers_sha256="2" * 64,
        request_contract_sha256="3" * 64,
        request_lineage_sha256="4" * 64,
        pagination_lineage_sha256="5" * 64,
        raw_landed_before_parse=True,
    )
    evidence.validate()
    with pytest.raises(ContractError, match="land before parse"):
        replace(evidence, raw_landed_before_parse=False).validate()
    with pytest.raises(ContractError, match="page hash census"):
        replace(evidence, page_sha256s=evidence.page_sha256s[:1]).validate()
    with pytest.raises(ContractError, match="page census ID differs"):
        replace(evidence, page_sha256s=tuple(reversed(evidence.page_sha256s))).validate()
    with pytest.raises(ContractError, match="ordered page composition"):
        ProviderLineageEvidence.create(
            raw_bytes=b"not-the-pages",
            page_payloads=pages,
            expected_page_count=2,
            response_headers_sha256="2" * 64,
            request_contract_sha256="3" * 64,
            request_lineage_sha256="4" * 64,
            pagination_lineage_sha256="5" * 64,
            raw_landed_before_parse=True,
        )


def _prospective_protocol(
    *,
    current_session: int = 99,
    early_stop_policy_id: str = "6" * 64,
) -> ProspectiveControlProtocol:
    return ProspectiveControlProtocol.create(
        maximum_attempts=2,
        fixed_end_session=100,
        current_session=current_session,
        sealed_before_first_prediction=True,
        aggregate_blinded_until_fixed_end=True,
        missed_vintage_backfill_allowed=False,
        indirect_holdout_queries_allowed=False,
        failed_holdout_reuse_allowed=False,
        early_stop_policy_id=early_stop_policy_id,
        expected_vintage_ids=("v1", "v2"),
    )


def test_optional_stop_fixed_end_and_missed_vintages_fail_closed() -> None:
    initial = _prospective_protocol()
    first_attempt = require_next_attempt(initial)
    assert first_attempt.attempts_used == 1
    assert first_attempt.previous_protocol_id == initial.protocol_id
    assert first_attempt.protocol_id != initial.protocol_id
    with pytest.raises(ContractError, match="already consumed"):
        require_next_attempt(initial)
    second_attempt = require_next_attempt(first_attempt)
    assert second_attempt.attempts_used == 2
    assert second_attempt.previous_protocol_id == first_attempt.protocol_id
    with pytest.raises(ContractError, match="attempt budget is exhausted"):
        require_next_attempt(second_attempt)
    with pytest.raises(ContractError, match="remains blinded"):
        authorize_aggregate_read(initial)

    complete = _prospective_protocol(
        current_session=100,
        early_stop_policy_id="8" * 64,
    )
    authorize_aggregate_read(complete)
    assert (
        verify_prospective_vintage_census(
            complete,
            observed_vintage_ids=("v1", "v2"),
            backfilled_vintage_ids=(),
        )
        == "PASS_PROSPECTIVE_CENSUS_MECHANICS_ONLY"
    )
    with pytest.raises(ContractError, match="cannot be backfilled"):
        verify_prospective_vintage_census(
            complete,
            observed_vintage_ids=("v1", "v2"),
            backfilled_vintage_ids=("v2",),
        )
    with pytest.raises(ContractError, match="out-of-order"):
        verify_prospective_vintage_census(
            complete,
            observed_vintage_ids=("v2", "v1"),
            backfilled_vintage_ids=(),
        )
    assert verify_prospective_vintage_census(
        complete,
        observed_vintage_ids=(),
        backfilled_vintage_ids=(),
    ) == "INCONCLUSIVE_MISSED_VINTAGES"
    with pytest.raises(ContractError, match="protocol ID differs"):
        replace(complete, expected_vintage_ids=("v1",)).validate()
    values = complete.unsigned_dict()
    values.pop("attempts_used")
    values.pop("previous_protocol_id")
    values["expected_vintage_ids"] = ()
    with pytest.raises(ContractError, match="cannot be empty"):
        ProspectiveControlProtocol.create(**values)


def test_indirect_and_reused_holdout_access_is_rejected() -> None:
    protocol = _prospective_protocol(early_stop_policy_id="9" * 64)
    permit = SyntheticOnlyPermit.create(
        fixture_id="meta-audit-holdout-reuse",
        scope="TRUSTED_CLOCK_FIXED_TIME",
    )
    clock = TrustedClock.synthetic_fixed(
        datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc),
        permit=permit,
    )
    locked = build_holdout_receipt(trial_id="7" * 64, state="LOCKED", clock=clock)
    with pytest.raises(ContractError, match="indirect holdout"):
        require_direct_final_holdout_query(
            protocol,
            query_kind="INDIRECT_AGGREGATE_QUERY",
            holdout_receipt=locked,
        )
    with pytest.raises(ContractError, match="exact authorized unlock"):
        require_direct_final_holdout_query(
            protocol,
            query_kind="DIRECT_REGISTERED_FINAL_HOLDOUT",
            holdout_receipt=False,  # type: ignore[arg-type]
        )
    with pytest.raises(ContractError, match="exact authorized unlock"):
        require_direct_final_holdout_query(
            protocol,
            query_kind="DIRECT_REGISTERED_FINAL_HOLDOUT",
            holdout_receipt=locked,
        )
    unlocked = build_holdout_receipt(
        trial_id="7" * 64,
        state="UNLOCKED_ONCE",
        clock=clock,
        previous=locked,
    )
    authorization_id = require_direct_final_holdout_query(
        protocol,
        query_kind="DIRECT_REGISTERED_FINAL_HOLDOUT",
        holdout_receipt=unlocked,
    )
    assert len(authorization_id) == 64
    with pytest.raises(ContractError, match="already consumed"):
        require_direct_final_holdout_query(
            protocol,
            query_kind="DIRECT_REGISTERED_FINAL_HOLDOUT",
            holdout_receipt=unlocked,
        )
    with pytest.raises(EvaluationAuthorizationError, match="unlocked only once"):
        build_holdout_receipt(
            trial_id="7" * 64,
            state="UNLOCKED_ONCE",
            clock=clock,
            previous=unlocked,
        )
    closed = build_holdout_receipt(
        trial_id="7" * 64,
        state="CLOSED",
        clock=clock,
        previous=unlocked,
    )
    with pytest.raises(EvaluationAuthorizationError, match="close only after"):
        build_holdout_receipt(
            trial_id="7" * 64,
            state="CLOSED",
            clock=clock,
            previous=closed,
        )


def _economic_books(
    *,
    missing_session: int | None = None,
    adv_notional: float | None = 10_000_000.0,
) -> tuple[DailyCohortBook, ...]:
    definitions = {
        "c1": (1, "stock_long", "A", 0.2),
        "c2": (2, "stock_short", "B", -0.2),
        "c3": (3, "etf_long", "C", 0.2),
        "c4": (4, "etf_short", "D", -0.2),
        "c5": (5, "stock_long", "E", 0.2),
        "c6": (6, "stock_long", "A", 0.2),
    }
    books: list[DailyCohortBook] = []
    for session in range(1, 11):
        active = {
            cohort: values
            for cohort, values in definitions.items()
            if values[0] <= session <= values[0] + 4
        }
        weights = {
            cohort: {asset: weight}
            for cohort, (_, _, asset, weight) in active.items()
        }
        sleeves = {cohort: sleeve for cohort, (_, sleeve, _, _) in active.items()}
        returns: dict[str, float | None] = {
            asset: 0.01 for _, _, asset, _ in definitions.values()
        }
        if missing_session == session:
            returns["A"] = None
        adv = {asset: adv_notional for _, _, asset, _ in definitions.values()}
        books.append(
            DailyCohortBook(
                session=session,
                cohort_weights=weights,
                cohort_sleeves=sleeves,
                asset_returns=returns,
                asset_adv_notional=adv,
            )
        )
    return tuple(books)


def test_five_cohort_oracle_uses_actual_weight_turnover_and_monotonic_costs() -> None:
    result = reconstruct_five_cohort_economics(_economic_books())
    assert result.mechanics_only is True
    assert result.required_sleeves == (
        "stock_long",
        "stock_short",
        "etf_long",
        "etf_short",
    )
    assert result.rows[5].session == 6
    assert result.rows[5].turnover == pytest.approx(0.0)
    assert result.capacity_status == "UNKNOWN"
    assert result.borrow_status == "GROSS_ONLY_BORROW_EXCLUDED"
    for row in result.rows:
        values = [value for _, value in row.net_returns]
        assert values == sorted(values, reverse=True)


def test_five_cohort_oracle_preserves_unavailable_and_capacity_states() -> None:
    unavailable = reconstruct_five_cohort_economics(
        _economic_books(missing_session=5)
    )
    row = next(value for value in unavailable.rows if value.session == 5)
    assert row.outcome_status == "UNAVAILABLE_RETURN"
    assert row.gross_return is None
    assert all(value is None for _, value in row.net_returns)

    passing_capacity = reconstruct_five_cohort_economics(
        _economic_books(adv_notional=10_000_000.0),
        policy=EconomicPolicy(
            portfolio_notional=1_000_000.0,
            maximum_adv_participation=0.10,
        ),
    )
    assert passing_capacity.capacity_status == "PASS"

    failing_capacity = reconstruct_five_cohort_economics(
        _economic_books(adv_notional=1_000_000.0),
        policy=EconomicPolicy(
            portfolio_notional=1_000_000.0,
            maximum_adv_participation=0.10,
        ),
    )
    assert failing_capacity.capacity_status == "FAIL"


def test_five_cohort_oracle_rejects_overallocated_cohort() -> None:
    books = list(_economic_books())
    first = books[0]
    books[0] = replace(first, cohort_weights={"c1": {"A": 0.21}})
    with pytest.raises(ResearchContractError, match="one-fifth capital"):
        reconstruct_five_cohort_economics(tuple(books))

    direction_books = list(_economic_books())
    short_book = direction_books[1]
    direction_books[1] = replace(
        short_book,
        cohort_weights={"c1": {"A": 0.2}, "c2": {"B": 0.2}},
    )
    with pytest.raises(ResearchContractError, match="short sleeve contains a long weight"):
        reconstruct_five_cohort_economics(tuple(direction_books))


def test_traceability_binds_reusable_synthetic_audit_controls_directly() -> None:
    root = Path(__file__).resolve().parents[1]
    traceability = (root / "docs" / "AUDIT_TRACEABILITY.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "`audit_controls.py`",
        "`trials.py`",
        "`research/economics.py`",
        "`tests/test_meta_audit_remediation.py`",
        "`caches`, `artifacts`, and `admitted_evidence`",
        "retained real-project scan requires separate authorization",
        "no provider request or source activation",
        "no alpha, evaluation, capacity, or deployment claim",
    ):
        assert required in traceability


def test_retired_audit_specs_and_configs_are_absent_and_non_authorizing() -> None:
    root = Path(__file__).resolve().parents[1]
    retired = (
        "MASTER_AUDIT.md",
        "META_MASTER_AUDIT.md",
        "config/master_audit_policy.json",
        "config/meta_audit_reference_corpus.json",
    )
    assert all(not (root / relative).exists() for relative in retired)

    workflow = (root / "docs/AUDIT_WORKFLOW.md").read_text(encoding="utf-8")
    normalized = " ".join(workflow.split())
    assert "Historical versions remain available through Git history" in normalized
    assert "does not restore the retired interfaces" in normalized
    assert "must not reuse or infer authority" in normalized


def test_meta_audit_v2_groups_are_bounded_blind_first_and_identity_bound(
    tmp_path: Path,
) -> None:
    reference_path = tmp_path / "reference.txt"
    target_path = tmp_path / "target.txt"
    reference_path.write_text(
        "".join(f"reference {index:04d}\n" for index in range(450)),
        encoding="utf-8",
        newline="\n",
    )
    target_path.write_text("target\n", encoding="utf-8", newline="\n")
    reference = _meta_binding(tmp_path, "reference.txt")
    target = _meta_binding(tmp_path, "target.txt")

    groups = meta_audit_harness.build_maximal_read_groups(
        root=tmp_path,
        reference_bindings=(reference,),
        target_binding=target,
    )

    assert [group.group_ordinal for group in groups] == list(
        range(1, len(groups) + 1)
    )
    phases = [group.phase for group in groups]
    first_target = phases.index("TARGET")
    assert set(phases[:first_target]) == {"REFERENCE"}
    assert set(phases[first_target:]) == {"TARGET"}
    assert sum(
        item.line_count
        for group in groups
        for item in group.slices
        if group.phase == "REFERENCE"
    ) == 450
    assert all(
        group.rendered_line_count <= meta_audit_harness.MAX_GROUP_LINES
        and group.rendered_utf8_bytes <= meta_audit_harness.MAX_GROUP_UTF8_BYTES
        for group in groups
    )

    reference_path.write_text("changed\n", encoding="utf-8", newline="\n")
    with pytest.raises(IntegrityError, match="REFERENCE_IDENTITY_MISMATCH"):
        meta_audit_harness.build_maximal_read_groups(
            root=tmp_path,
            reference_bindings=(reference,),
            target_binding=target,
        )


def test_meta_audit_v2_rejects_non_lf_and_unrenderable_reference_text(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "target.txt"
    target_path.write_bytes(b"target\n")
    target = _meta_binding(tmp_path, "target.txt")

    reference_path = tmp_path / "reference.txt"
    reference_path.write_bytes(b"one\r\ntwo\r\n")
    with pytest.raises(ContractError, match="NON_LF_TEXT"):
        meta_audit_harness.build_maximal_read_groups(
            root=tmp_path,
            reference_bindings=(_meta_binding(tmp_path, "reference.txt"),),
            target_binding=target,
        )

    reference_path.write_bytes(b"x" * meta_audit_harness.MAX_GROUP_UTF8_BYTES)
    with pytest.raises(ContractError, match="UNRENDERABLE_LINE"):
        meta_audit_harness.build_maximal_read_groups(
            root=tmp_path,
            reference_bindings=(_meta_binding(tmp_path, "reference.txt"),),
            target_binding=target,
        )


def test_meta_audit_v2_dispatch_is_canonical_and_rejects_semantic_tampering(
    tmp_path: Path,
) -> None:
    envelope = _meta_v2_envelope(tmp_path)
    envelope_path = (tmp_path / "envelope.json").resolve()
    envelope_sha256 = "e" * 64
    dispatch = meta_audit_harness.build_reviewer_dispatch(
        envelope,
        envelope_path=envelope_path,
        envelope_sha256=envelope_sha256,
    )

    rendered = meta_audit_harness.canonical_reviewer_dispatch_bytes(dispatch)
    assert rendered == canonical_json_bytes(dispatch)
    assert b"target one" not in rendered
    assert b"reference one" not in rendered

    tampered = dict(dispatch)
    tampered_commands = [dict(command) for command in dispatch["commands"]]
    tampered_commands[2]["required_stdout_footer"] = None
    tampered["commands"] = tampered_commands
    unsigned = {key: value for key, value in tampered.items() if key != "dispatch_id"}
    tampered["dispatch_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    with pytest.raises(ContractError, match="FOOTER_MISMATCH"):
        meta_audit_harness.canonical_reviewer_dispatch_bytes(tampered)
