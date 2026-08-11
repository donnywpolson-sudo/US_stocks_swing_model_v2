from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..alpaca_free_bounded import (
    EvidenceClass,
    PROFILE_ID,
    build_calendar_qualification_plan,
    build_historical_backfill_plan,
    execute_calendar_qualification_cutover,
    load_profile,
)
from ..bounded_reporting import (
    AcquisitionCoverage,
    ReadinessInputs,
    assess_readiness,
    build_event_status_report,
)
from ..bounded_universe import (
    IdentityEvidence,
    LiquidityObservation,
    SENSITIVITY_PROFILE,
    UniverseCandidate,
    build_universe_snapshot,
)
from ..clock import TrustedClock
from ..free_acquisition import execute_one_source_request
from ..free_source_evidence import (
    RawEvidenceStore,
    append_capture_ledger_entry,
    alpha_vantage_listing_plan,
    alpaca_bars_plan,
    alpaca_sip_access_plan,
    build_daily_capture_plan,
    build_t_minus_one_operational_schedule,
    build_prospective_universe_snapshot,
    prospective_source_plans,
    validate_capture_ledger,
)
from ..long_short import PositionOutcome
from ..local_credentials import load_local_api_env
from ..providers.snapshots import NetworkAcquisitionRegistry
from ..prospective_liquidity_warmup import (
    build_liquidity_universe_snapshot,
    build_liquidity_warmup_plan,
    execute_liquidity_warmup,
    load_warmup_checkpoint,
    start_soak_generation,
    validate_soak_generations,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _prospective_plan(root: Path, source: str, as_of: date):
    plans = {plan.source: plan for plan in prospective_source_plans(repository_root=root, observed_for=as_of)}
    aliases = {
        "assets": "alpaca_free_bounded_assets",
        "corporate-actions": "alpaca_free_bounded_corporate_actions",
        "nasdaq-listed": "nasdaq_free_bounded_listed",
        "nasdaq-other": "nasdaq_free_bounded_otherlisted",
    }
    return plans[aliases[source]]


def _identity(payload: dict[str, object]) -> IdentityEvidence:
    return IdentityEvidence(
        stable_asset_id=str(payload["stable_asset_id"]),
        provider_asset_id=str(payload["provider_asset_id"]),
        original_requested_ticker=str(payload["original_requested_ticker"]),
        returned_ticker=str(payload["returned_ticker"]),
        source_ticker=str(payload["source_ticker"]),
        requested_as_of=date.fromisoformat(str(payload["requested_as_of"])),
        ticker_effective_from=date.fromisoformat(str(payload["ticker_effective_from"])),
        ticker_effective_through=(
            date.fromisoformat(str(payload["ticker_effective_through"]))
            if payload.get("ticker_effective_through") else None
        ),
        listing_from=(date.fromisoformat(str(payload["listing_from"])) if payload.get("listing_from") else None),
        delisting_through=(
            date.fromisoformat(str(payload["delisting_through"])) if payload.get("delisting_through") else None
        ),
        exchange=str(payload["exchange"]),
        effective_at=_utc(str(payload["effective_at"])),
        known_at=_utc(str(payload["known_at"])),
        mapping_evidence_id=str(payload["mapping_evidence_id"]),
        mapping_status=str(payload["mapping_status"]),
        evidence_class=EvidenceClass(str(payload["evidence_class"])),
    )


def _capture_phase(value: str) -> str:
    mapping = {
        "pre-decision": "PRE_DECISION",
        "completed-session": "COMPLETED_SESSION",
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("capture phase is invalid") from exc


def _candidate(payload: dict[str, object]) -> UniverseCandidate:
    flags = {
        name: bool(payload.get(name, False))
        for name in (
            "is_etf_or_etp",
            "is_adr",
            "is_preferred_warrant_right_or_unit",
            "is_closed_end_or_mutual_fund",
            "is_structured_product",
            "is_leveraged_or_inverse",
            "is_test_issue",
            "is_otc",
        )
    }
    return UniverseCandidate(
        identity=_identity(dict(payload["identity"])),
        ticker=str(payload["ticker"]),
        security_classification=str(payload["security_classification"]),
        exchange=str(payload["exchange"]),
        source_memberships=tuple(str(value) for value in payload["source_memberships"]),
        source_receipt_times=tuple(_utc(str(value)) for value in payload["source_receipt_times"]),
        observations=tuple(
            LiquidityObservation(
                session=date.fromisoformat(str(row["session"])),
                close=float(row["close"]),
                volume=float(row["volume"]),
                available_at=_utc(str(row["available_at"])),
                source_hash=str(row["source_hash"]),
            )
            for row in payload["observations"]
        ),
        evidence_hashes=tuple(str(value) for value in payload["evidence_hashes"]),
        **flags,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ALPACA_FREE_BOUNDED_V1 data, identity, universe, outcome, and readiness controls"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config")
    subparsers.add_parser(
        "validate-credentials",
        help="report canonical credential names and presence only; never display values",
    )
    subparsers.add_parser("plan-calendar-qualification")
    qualify_calendar = subparsers.add_parser("qualify-calendar-successor")
    qualify_calendar.add_argument("--approved-plan-id", required=True)
    qualify_calendar.add_argument("--owner-confirmation", required=True)

    daily = subparsers.add_parser("plan-daily-capture")
    daily.add_argument("--session", type=date.fromisoformat, required=True)
    daily.add_argument(
        "--phase", choices=("pre-decision", "completed-session"), required=True
    )
    daily.add_argument("--symbol", action="append", default=[])
    operational = subparsers.add_parser("plan-operational-capture")
    operational.add_argument("--session", type=date.fromisoformat, required=True)
    probe = subparsers.add_parser("probe-capabilities")
    probe.add_argument("--as-of", type=date.fromisoformat, required=True)

    backfill = subparsers.add_parser("plan-backfill")
    backfill.add_argument("--symbol", action="append", required=True)
    backfill.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    backfill.add_argument("--end", type=date.fromisoformat, required=True)
    backfill.add_argument("--requested-at", type=_utc, required=True)
    backfill.add_argument("--completed-unit-id", action="append", default=[])
    backfill.add_argument("--full", action="store_true")

    resume = subparsers.add_parser("resume-backfill")
    for action in backfill._actions[1:]:
        if action.dest != "full":
            resume._add_action(action)
    resume.add_argument("--full", action="store_true")

    premarket = subparsers.add_parser("capture-premarket")
    premarket.add_argument("--as-of", type=date.fromisoformat, required=True)

    completed = subparsers.add_parser("capture-completed-session")
    completed.add_argument("--session", type=date.fromisoformat, required=True)
    completed.add_argument("--symbol", action="append", required=True)

    sip_access = subparsers.add_parser("plan-sip-access")
    sip_access.add_argument("--start-at", type=_utc, required=True)
    sip_access.add_argument("--end-at", type=_utc, required=True)
    sip_access.add_argument("--endpoint-form", choices=("single", "multi"), required=True)

    execute = subparsers.add_parser("execute-source")
    execute.add_argument(
        "--source",
        choices=("bars", "sip-single", "sip-multi", "alpha-active", "alpha-delisted", "assets", "corporate-actions", "nasdaq-listed", "nasdaq-other"),
        required=True,
    )
    execute.add_argument("--as-of", type=date.fromisoformat, required=True)
    execute.add_argument("--symbol", action="append", default=[])
    execute.add_argument("--end-exclusive", type=date.fromisoformat)
    execute.add_argument("--start-at", type=_utc)
    execute.add_argument("--end-at", type=_utc)
    execute.add_argument("--approved-plan-id", required=True)
    execute.add_argument("--execute-network", action="store_true", required=True)
    execute.add_argument("--page-index", type=int, default=0)
    execute.add_argument("--page-token")
    execute.add_argument("--retry-attempt", type=int, default=1)
    execute.add_argument("--parent-request-id")
    execute.add_argument(
        "--prospective",
        action="store_true",
        help="classify a current completed-session bars request as prospectively observed",
    )
    execute.add_argument(
        "--evidence-root",
        type=Path,
        default=_root() / "data" / "vault" / "qualification" / "as_received" / "alpaca_free_bounded_v1",
    )

    receipts = subparsers.add_parser("validate-receipts")
    receipts.add_argument(
        "--evidence-root",
        type=Path,
        default=_root() / "data" / "vault" / "qualification" / "as_received" / "alpaca_free_bounded_v1",
    )

    prospective_universe = subparsers.add_parser("rebuild-prospective-universe")
    prospective_universe.add_argument("--session", type=date.fromisoformat, required=True)
    prospective_universe.add_argument("--receipt-id", action="append", required=True)
    prospective_universe.add_argument(
        "--evidence-root",
        type=Path,
        default=_root() / "data" / "vault" / "qualification" / "as_received" / "alpaca_free_bounded_v1",
    )
    prospective_universe.add_argument(
        "--output-root",
        type=Path,
        default=_root() / "data" / "w" / "alpaca_free_bounded_v1" / "prospective_universe",
    )

    ledger = subparsers.add_parser("record-daily-capture")
    ledger.add_argument("--session", type=date.fromisoformat, required=True)
    ledger.add_argument(
        "--phase", choices=("pre-decision", "completed-session"), required=True
    )
    ledger.add_argument("--symbol", action="append", default=[])
    ledger.add_argument("--receipt-id", action="append", required=True)
    ledger.add_argument("--universe-snapshot-id")
    ledger.add_argument(
        "--evidence-root",
        type=Path,
        default=_root() / "data" / "vault" / "qualification" / "as_received" / "alpaca_free_bounded_v1",
    )
    ledger.add_argument(
        "--ledger-path",
        type=Path,
        default=_root() / "data" / "w" / "alpaca_free_bounded_v1" / "prospective_capture_ledger.jsonl",
    )
    validate_ledger = subparsers.add_parser("validate-capture-ledger")
    validate_ledger.add_argument(
        "--ledger-path",
        type=Path,
        default=_root() / "data" / "w" / "alpaca_free_bounded_v1" / "prospective_capture_ledger.jsonl",
    )

    for command in ("plan-liquidity-warmup", "execute-liquidity-warmup", "validate-warmup-checkpoint", "build-liquidity-universe"):
        warmup = subparsers.add_parser(command)
        warmup.add_argument("--source-snapshot", type=Path, required=True)
        warmup.add_argument("--pilot-symbol-count", type=int)
        warmup.add_argument(
            "--checkpoint",
            type=Path,
            default=_root() / "data" / "w" / "alpaca_free_bounded_v1" / "liquidity_warmup_checkpoint.json",
        )
        if command == "execute-liquidity-warmup":
            warmup.add_argument("--approved-plan-id", required=True)
            warmup.add_argument("--execute-network", action="store_true", required=True)
        if command == "build-liquidity-universe":
            warmup.add_argument(
                "--output-root",
                type=Path,
                default=_root() / "data" / "w" / "alpaca_free_bounded_v1" / "liquidity_universe",
            )

    universe = subparsers.add_parser("rebuild-universe")
    universe.add_argument("--input", type=Path, required=True)
    universe.add_argument("--profile", choices=(PROFILE_ID, SENSITIVITY_PROFILE), default=PROFILE_ID)

    soak_start = subparsers.add_parser("start-soak-generation")
    soak_start.add_argument("--remediation-commit", required=True)
    soak_start.add_argument("--sip-availability-rule", required=True)
    soak_start.add_argument("--warmup-checkpoint-id", required=True)
    soak_start.add_argument("--universe-snapshot-id", required=True)
    soak_start.add_argument(
        "--original-ledger", type=Path,
        default=_root() / "data" / "w" / "alpaca_free_bounded_v1" / "prospective_capture_ledger.jsonl",
    )
    soak_start.add_argument(
        "--generation-ledger", type=Path,
        default=_root() / "data" / "w" / "alpaca_free_bounded_v1" / "prospective_soak_generations.jsonl",
    )
    soak_validate = subparsers.add_parser("validate-soak-generations")
    soak_validate.add_argument(
        "--generation-ledger", type=Path,
        default=_root() / "data" / "w" / "alpaca_free_bounded_v1" / "prospective_soak_generations.jsonl",
    )

    coverage = subparsers.add_parser("coverage-report")
    coverage.add_argument("--input", type=Path, required=True)
    events = subparsers.add_parser("event-status-report")
    events.add_argument("--input", type=Path, required=True)

    readiness = subparsers.add_parser("check-readiness")
    readiness.add_argument("--synthetic-tests-passed", action="store_true")
    readiness.add_argument("--alpaca-live-validated", action="store_true")
    readiness.add_argument("--alpha-vantage-semantics-validated", action="store_true")
    readiness.add_argument("--prospective-daily-capture-validated", action="store_true")
    readiness.add_argument("--prospective-short-gate-validated-live", action="store_true")
    subparsers.add_parser("known-case-diagnostics")

    args = parser.parse_args(argv)
    root = _root()
    if args.command == "validate-config":
        profile = load_profile(root)
        registry = NetworkAcquisitionRegistry.load(root / "config/alpaca_free_bounded_network_registry.json", allowed_root=root)
        _print({"state": "PASS", "profile_id": profile["profile_id"], "network_registry_id": registry.registry_id})
        return 0
    if args.command == "validate-credentials":
        result = load_local_api_env(root)
        _print({
            "state": "PASS" if all(result["presence"].values()) else "MISSING_CREDENTIALS",
            "canonical_variables": list(result["presence"]),
            "presence": result["presence"],
            "loader_state": result["state"],
        })
        return 0
    if args.command == "plan-calendar-qualification":
        _print(build_calendar_qualification_plan(repository_root=root))
        return 0
    if args.command == "qualify-calendar-successor":
        receipt = execute_calendar_qualification_cutover(
            repository_root=root,
            approved_plan_id=args.approved_plan_id,
            owner_confirmation=args.owner_confirmation,
            clock=TrustedClock.production(),
        )
        _print(receipt)
        return 0
    if args.command == "plan-daily-capture":
        phase = _capture_phase(args.phase)
        plan = build_daily_capture_plan(
            repository_root=root,
            session=args.session,
            phase=phase,
            symbols=args.symbol,
        )
        _print({"state": "PLAN_ONLY_NO_NETWORK", **plan.as_dict()})
        return 0
    if args.command == "plan-operational-capture":
        _print({"state": "PLAN_ONLY_NO_NETWORK", **build_t_minus_one_operational_schedule(
            repository_root=root, signal_session=args.session
        )})
        return 0
    if args.command == "probe-capabilities":
        plans = list(prospective_source_plans(repository_root=root, observed_for=args.as_of))
        plans.extend(
            alpha_vantage_listing_plan(repository_root=root, as_of=args.as_of, state=state)
            for state in ("active", "delisted")
        )
        _print({
            "state": "PLAN_ONLY_NO_NETWORK",
            "plans": [plan.as_dict() for plan in plans],
            "credentials_read": False,
            "live_diagnostics": "NOT_RUN_PLAN_ONLY",
        })
        return 0
    if args.command in {"plan-backfill", "resume-backfill"}:
        plan = build_historical_backfill_plan(
            repository_root=root,
            symbols=args.symbol,
            requested_start=args.start,
            requested_end=args.end,
            requested_at=args.requested_at,
            completed_unit_ids=args.completed_unit_id,
        )
        payload = plan.summary()
        if args.full:
            payload["units"] = [unit.receipt_dict() for unit in plan.units]
            payload["pending_unit_ids"] = [unit.unit_id for unit in plan.pending_units]
        _print(payload)
        return 0
    if args.command == "capture-premarket":
        plans = prospective_source_plans(repository_root=root, observed_for=args.as_of)
        selected = [plan for plan in plans if plan.source != "alpaca_free_bounded_corporate_actions"]
        _print({"state": "PLAN_ONLY_NO_NETWORK", "ordered_plans": [plan.as_dict() for plan in selected]})
        return 0
    if args.command == "capture-completed-session":
        plan = alpaca_bars_plan(
            repository_root=root,
            symbols=args.symbol,
            start=args.session,
            end_exclusive=args.session + timedelta(days=1),
            evidence_class=EvidenceClass.PROSPECTIVE_AS_OBSERVED,
        )
        actions = _prospective_plan(root, "corporate-actions", args.session)
        _print({"state": "PLAN_ONLY_NO_NETWORK", "ordered_plans": [plan.as_dict(), actions.as_dict()]})
        return 0
    if args.command == "plan-sip-access":
        plan = alpaca_sip_access_plan(
            repository_root=root,
            start_at=args.start_at,
            end_at=args.end_at,
            endpoint_form=args.endpoint_form,
        )
        _print({"state": "PLAN_ONLY_NO_NETWORK", **plan.as_dict()})
        return 0
    if args.command == "execute-source":
        load_local_api_env(root)
        if args.source in {"sip-single", "sip-multi"}:
            if args.start_at is None or args.end_at is None:
                parser.error("SIP access execution requires --start-at and --end-at")
            plan = alpaca_sip_access_plan(
                repository_root=root,
                start_at=args.start_at,
                end_at=args.end_at,
                endpoint_form=args.source.removeprefix("sip-"),
            )
        elif args.source == "bars":
            if not args.symbol or args.end_exclusive is None:
                parser.error("bars execution requires --symbol and --end-exclusive")
            plan = alpaca_bars_plan(
                repository_root=root,
                symbols=args.symbol,
                start=args.as_of,
                end_exclusive=args.end_exclusive,
                evidence_class=(
                    EvidenceClass.PROSPECTIVE_AS_OBSERVED
                    if args.prospective
                    else EvidenceClass.HISTORICAL_RECONSTRUCTED
                ),
            )
        elif args.source.startswith("alpha-"):
            plan = alpha_vantage_listing_plan(
                repository_root=root,
                as_of=args.as_of,
                state=args.source.removeprefix("alpha-"),
            )
        else:
            plan = _prospective_plan(root, args.source, args.as_of)
        evidence_root = args.evidence_root.resolve()
        allowed_root = (root / "data").resolve()
        store = RawEvidenceStore(evidence_root, allowed_root=allowed_root)
        registry = NetworkAcquisitionRegistry.load(
            root / "config/alpaca_free_bounded_network_registry.json",
            allowed_root=root,
        )
        result = execute_one_source_request(
            plan=plan,
            approved_plan_id=args.approved_plan_id,
            evidence_store=store,
            network_registry=registry,
            clock=TrustedClock.production(),
            network_enabled=args.execute_network,
            alpaca_key_id=os.environ.get("APCA_API_KEY_ID"),
            alpaca_secret_key=os.environ.get("APCA_API_SECRET_KEY"),
            alpha_vantage_key=os.environ.get("ALPHA_VANTAGE_API_KEY"),
            page_index=args.page_index,
            requested_page_token=args.page_token,
            retry_attempt=args.retry_attempt,
            parent_request_id=args.parent_request_id,
        )
        _print(result.summary())
        return 0
    if args.command == "validate-receipts":
        store = RawEvidenceStore(args.evidence_root.resolve(), allowed_root=(root / "data").resolve())
        _print(store.validate())
        return 0
    if args.command == "rebuild-prospective-universe":
        store = RawEvidenceStore(args.evidence_root.resolve(), allowed_root=(root / "data").resolve())
        plan = build_daily_capture_plan(
            repository_root=root,
            session=args.session,
            phase="PRE_DECISION",
        )
        payload = build_prospective_universe_snapshot(
            plan=plan,
            evidence_store=store,
            receipt_ids=args.receipt_id,
            output_root=args.output_root.resolve(),
        )
        _print({
            "state": "PASS",
            "universe_snapshot_id": payload["universe_snapshot_id"],
            "candidate_count": payload["candidate_count"],
            "eligible_for_t_minus_1_liquidity_count": payload["eligible_for_t_minus_1_liquidity_count"],
            "selection_state": payload["selection_state"],
        })
        return 0
    if args.command == "record-daily-capture":
        store = RawEvidenceStore(args.evidence_root.resolve(), allowed_root=(root / "data").resolve())
        plan = build_daily_capture_plan(
            repository_root=root,
            session=args.session,
            phase=_capture_phase(args.phase),
            symbols=args.symbol,
        )
        _print(append_capture_ledger_entry(
            plan=plan,
            evidence_store=store,
            receipt_ids=args.receipt_id,
            ledger_path=args.ledger_path.resolve(),
            universe_snapshot_id=args.universe_snapshot_id,
            appended_at=TrustedClock.production().now(),
        ))
        return 0
    if args.command == "validate-capture-ledger":
        _print(validate_capture_ledger(ledger_path=args.ledger_path.resolve()))
        return 0
    if args.command in {"plan-liquidity-warmup", "execute-liquidity-warmup", "validate-warmup-checkpoint", "build-liquidity-universe"}:
        plan = build_liquidity_warmup_plan(
            repository_root=root,
            source_snapshot_path=args.source_snapshot,
            pilot_symbol_count=args.pilot_symbol_count,
        )
        if args.command == "plan-liquidity-warmup":
            _print({
                "state": "PLAN_ONLY_NO_NETWORK",
                "warmup_plan_id": plan.warmup_plan_id,
                "source_snapshot_id": plan.source_snapshot_id,
                "session_count": len(plan.sessions),
                "symbol_count": sum(len(unit.symbols) for unit in plan.units),
                "unit_count": len(plan.units),
                "pilot_symbol_count": plan.pilot_symbol_count,
                "units": [unit.as_dict() for unit in plan.units],
            })
            return 0
        if args.command == "validate-warmup-checkpoint":
            checkpoint = load_warmup_checkpoint(args.checkpoint.resolve(), plan)
            _print({
                "state": "PASS",
                "checkpoint_id": checkpoint["checkpoint_id"],
                "complete": checkpoint["complete"],
                "completed_unit_count": len(checkpoint["completed_units"]),
                "unit_count": len(plan.units),
            })
            return 0
        store = RawEvidenceStore(
            (root / "data" / "vault" / "qualification" / "as_received" / "alpaca_free_bounded_v1").resolve(),
            allowed_root=(root / "data").resolve(),
        )
        if args.command == "execute-liquidity-warmup":
            load_local_api_env(root)
            registry = NetworkAcquisitionRegistry.load(
                root / "config/alpaca_free_bounded_network_registry.json", allowed_root=root
            )
            _print(execute_liquidity_warmup(
                plan=plan,
                approved_plan_id=args.approved_plan_id,
                checkpoint_path=args.checkpoint.resolve(),
                evidence_store=store,
                network_registry=registry,
                clock=TrustedClock.production(),
                network_enabled=args.execute_network,
                alpaca_key_id=os.environ.get("APCA_API_KEY_ID"),
                alpaca_secret_key=os.environ.get("APCA_API_SECRET_KEY"),
            ))
            return 0
        payload = build_liquidity_universe_snapshot(
            plan=plan,
            checkpoint_path=args.checkpoint.resolve(),
            evidence_store=store,
            output_root=args.output_root.resolve(),
        )
        _print({
            "state": "PASS",
            "universe_snapshot_id": payload["universe_snapshot_id"],
            "candidate_count": payload["candidate_count"],
            "liquidity_ready_count": payload["liquidity_ready_count"],
            "selected_count": payload["selected_count"],
            "rank_cutoff": payload["rank_cutoff"],
            "evidence_class_composition": payload["evidence_class_composition"],
        })
        return 0
    if args.command == "rebuild-universe":
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        snapshot = build_universe_snapshot(
            profile_id=args.profile,
            signal_session=date.fromisoformat(payload["signal_session"]),
            information_cutoff_session=date.fromisoformat(payload["information_cutoff_session"]),
            decision_at=_utc(payload["decision_at"]),
            candidates=(_candidate(dict(item)) for item in payload["candidates"]),
        )
        _print(snapshot.as_dict())
        return 0
    if args.command == "start-soak-generation":
        _print(start_soak_generation(
            repository_root=root,
            original_ledger_path=args.original_ledger.resolve(),
            generation_ledger_path=args.generation_ledger.resolve(),
            remediation_commit=args.remediation_commit,
            sip_availability_rule=args.sip_availability_rule,
            warmup_checkpoint_id=args.warmup_checkpoint_id,
            universe_snapshot_id=args.universe_snapshot_id,
            started_at=TrustedClock.production().now(),
        ))
        return 0
    if args.command == "validate-soak-generations":
        _print(validate_soak_generations(args.generation_ledger.resolve(), repository_root=root))
        return 0
    if args.command == "coverage-report":
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        rows = [AcquisitionCoverage(**item) for item in payload]
        _print({"provider_acquisition_coverage": [row.as_dict() for row in rows]})
        return 0
    if args.command == "event-status-report":
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        _print(build_event_status_report(PositionOutcome.from_dict(dict(item)) for item in payload))
        return 0
    if args.command == "check-readiness":
        passed = args.synthetic_tests_passed
        _print(assess_readiness(ReadinessInputs(
            adapters_implemented=True,
            configuration_validated=True,
            credential_redaction_validated=True,
            append_only_receipts_validated=passed,
            complete_pagination_validated=passed,
            retry_resume_validated=passed,
            feed_adjustment_enforced=True,
            evidence_classes_validated=True,
            identity_continuity_validated=passed,
            universe_logic_validated=passed,
            long_short_outcomes_validated=passed,
            stress_reporting_validated=passed,
            denominator_reconciliation_validated=passed,
            synthetic_tests_passed=passed,
            alpaca_live_validated=args.alpaca_live_validated,
            alpha_vantage_semantics_validated=args.alpha_vantage_semantics_validated,
            prospective_daily_capture_validated=args.prospective_daily_capture_validated,
            prospective_short_gate_validated_live=args.prospective_short_gate_validated_live,
        )))
        return 0
    if args.command == "known-case-diagnostics":
        cases = [
            ("AAPL", "2020-08-31", "SPLIT_NEUTRAL_MECHANICS"),
            ("FB/META", "2022", "STABLE_IDENTITY_TICKER_CONTINUITY"),
            ("ATVI", "2023", "VERIFIED_CASH_CONSIDERATION_REQUIRED"),
            ("LK/LKNCY", "2020", "UNRESOLVED_OTC_CONTINUATION_WHEN_EXIT_UNAVAILABLE"),
            ("BBBY/BBBYQ", "2023", "ZERO_ONLY_WITH_EXPLICIT_ZERO_CONSIDERATION"),
        ]
        _print({
            "state": "LIVE_EVIDENCE_REQUIRES_RECEIPT_REVIEW",
            "cases": [
                {"case": case, "period": period, "required_principle": principle, "live_result": "NOT_IN_OFFLINE_COMMAND"}
                for case, period, principle in cases
            ],
            "synthetic_mechanics_available": True,
            "training_authorized": False,
            "evaluation_authorized": False,
        })
        return 0
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
