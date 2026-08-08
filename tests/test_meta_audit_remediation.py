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
from us_stocks_swing_model_v2.errors import ContractError, EvaluationAuthorizationError
from us_stocks_swing_model_v2.research.contracts import ResearchContractError
from us_stocks_swing_model_v2.research.economics import (
    DailyCohortBook,
    EconomicPolicy,
    reconstruct_five_cohort_economics,
)
from us_stocks_swing_model_v2.trials import build_holdout_receipt


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
    attempts_used: int = 0,
    current_session: int = 99,
) -> ProspectiveControlProtocol:
    return ProspectiveControlProtocol.create(
        maximum_attempts=2,
        attempts_used=attempts_used,
        fixed_end_session=100,
        current_session=current_session,
        sealed_before_first_prediction=True,
        aggregate_blinded_until_fixed_end=True,
        missed_vintage_backfill_allowed=False,
        indirect_holdout_queries_allowed=False,
        failed_holdout_reuse_allowed=False,
        early_stop_policy_id="6" * 64,
        expected_vintage_ids=("v1", "v2"),
    )


def test_optional_stop_fixed_end_and_missed_vintages_fail_closed() -> None:
    assert require_next_attempt(_prospective_protocol(attempts_used=1)) == 2
    with pytest.raises(ContractError, match="attempt budget is exhausted"):
        require_next_attempt(_prospective_protocol(attempts_used=2))
    with pytest.raises(ContractError, match="remains blinded"):
        authorize_aggregate_read(_prospective_protocol(current_session=99))

    complete = _prospective_protocol(current_session=100)
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
    values["expected_vintage_ids"] = ()
    with pytest.raises(ContractError, match="cannot be empty"):
        ProspectiveControlProtocol.create(**values)


def test_indirect_and_reused_holdout_access_is_rejected() -> None:
    protocol = _prospective_protocol()
    with pytest.raises(ContractError, match="indirect holdout"):
        require_direct_final_holdout_query(
            protocol,
            query_kind="INDIRECT_AGGREGATE_QUERY",
            holdout_unlocked=True,
        )
    with pytest.raises(ContractError, match="exact authorized unlock"):
        require_direct_final_holdout_query(
            protocol,
            query_kind="DIRECT_REGISTERED_FINAL_HOLDOUT",
            holdout_unlocked=False,
        )

    permit = SyntheticOnlyPermit.create(
        fixture_id="meta-audit-holdout-reuse",
        scope="TRUSTED_CLOCK_FIXED_TIME",
    )
    clock = TrustedClock.synthetic_fixed(
        datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc),
        permit=permit,
    )
    locked = build_holdout_receipt(trial_id="7" * 64, state="LOCKED", clock=clock)
    unlocked = build_holdout_receipt(
        trial_id="7" * 64,
        state="UNLOCKED_ONCE",
        clock=clock,
        previous=locked,
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


def test_audit_specs_bind_every_broader_remediation_control() -> None:
    root = Path(__file__).resolve().parents[1]
    master = (root / "MASTER_AUDIT.md").read_text(encoding="utf-8")
    meta = (root / "META_MASTER_AUDIT.md").read_text(encoding="utf-8")
    traceability = (root / "docs" / "AUDIT_TRACEABILITY.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "audit_controls.assess_traceability_matrix",
        "audit_controls.ProviderLineageEvidence",
        "audit_controls.ProspectiveControlProtocol",
        "research.economics.reconstruct_five_cohort_economics",
        "audit_controls.scan_declared_audit_surfaces",
        "`git`, `logs`, `reports`,",
        "`caches`, `artifacts`, and `admitted_evidence`",
    ):
        assert required in master

    for required in (
        "audit_controls.assess_traceability_matrix",
        "`audit_controls.ProspectiveControlProtocol`",
        "`research.economics.reconstruct_five_cohort_economics`",
        "`admitted_evidence`",
        "raw provider bytes",
    ):
        assert required in meta

    for required in (
        "`audit_controls.py`",
        "`research/economics.py`",
        "`tests/test_meta_audit_remediation.py`",
        "retained real-project scan requires separate authorization",
        "no provider request or source activation",
        "no alpha, evaluation, capacity, or deployment claim",
    ):
        assert required in traceability


def test_meta_audit_targets_the_master_specification_not_project_readiness() -> None:
    root = Path(__file__).resolve().parents[1]
    meta = (root / "META_MASTER_AUDIT.md").read_text(encoding="utf-8")
    normalized = " ".join(meta.split())

    for required in (
        "Version: `1.1.0`",
        "Mode: `MASTER_SPECIFICATION_REVIEW`",
        "exactly one review target",
        "They are not additional audit targets.",
        "are not Meta findings when the Master correctly requires",
        "`SPECIFICATION_SATISFACTORY`",
        "`SPECIFICATION_AMENDMENTS_REQUIRED`",
        "`SPECIFICATION_REVIEW_INCOMPLETE`",
        "unified diff against the frozen Master bytes",
        "reports/generated/meta_master_spec_review/<report-sha256>.md",
    ):
        assert required in normalized

    assert (
        "`SUPPORTABLE`, `BLOCKED`, and `INSUFFICIENT_EVIDENCE` are reserved "
        "for a later project-targeted Master Audit"
    ) in normalized
    assert "The review must not apply those amendments." in normalized
