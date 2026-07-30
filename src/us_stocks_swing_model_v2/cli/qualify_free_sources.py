from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request

from ..common import canonical_json_bytes, iso_z, sha256_bytes
from ..clock import TrustedClock
from ..errors import NetworkGuardError
from ..exchange_calendar import load_xnys_calendar_release
from ..providers.alpaca import (
    AUTH_ENVIRONMENT_TOKEN,
    AlpacaBarsPolicy,
    AlpacaBarsRequest,
    guarded_fetch_landed_pages,
    qualify_landed_pages,
)
from ..providers.nasdaq import NASDAQ_TRADED_URL, parse_nasdaq_traded
from ..providers.nasdaq_bootstrap import (
    load_nasdaq_bootstrap_policy,
    verify_nasdaq_bootstrap_pair,
)
from ..providers.http import open_without_redirects
from ..providers.network_execution import (
    NetworkRequestPlan,
    _bind_network_response,
    assert_local_network_request,
    start_local_network_execution,
)
from ..providers.snapshots import (
    ALLOWED_RESPONSE_HEADERS,
    AsReceivedSnapshotStore,
    NetworkAcquisitionRegistry,
    normalize_response_headers,
)


NASDAQ_URLS = (NASDAQ_TRADED_URL,)
MAX_NASDAQ_RESPONSE_BYTES = 32 * 1024 * 1024


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Plan or run bounded free-source qualification")
    mode = value.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan-only",
        action="store_true",
        help="print a request plan without network or filesystem writes; this is the default",
    )
    mode.add_argument("--execute-network", action="store_true", help=f"also requires {AUTH_ENVIRONMENT_TOKEN}=YES")
    mode.add_argument(
        "--verify-nasdaq-snapshot",
        type=Path,
        help="verify one already captured Nasdaq snapshot and its local integrity receipt",
    )
    mode.add_argument(
        "--verify-nasdaq-bootstrap-pair",
        nargs=2,
        type=Path,
        metavar=("SNAPSHOT_A", "SNAPSHOT_B"),
        help="offline-only verification of the frozen two-capture bootstrap pair",
    )
    mode.add_argument(
        "--verify-alpaca-pair",
        nargs=2,
        type=Path,
        metavar=("SIP_SNAPSHOT", "IEX_SNAPSHOT"),
        help="offline-only verification of one SIP and one IEX qualification snapshot",
    )
    selection = value.add_mutually_exclusive_group()
    selection.add_argument("--nasdaq-only", action="store_true")
    selection.add_argument("--alpaca-only", action="store_true")
    value.add_argument("--symbols", default="AAPL,SPY")
    value.add_argument("--start", default="2024-01-02T00:00:00Z")
    value.add_argument("--end", default="2024-01-10T00:00:00Z")
    value.add_argument("--max-pages", type=int, default=10)
    value.add_argument("--approved-sip-plan-id")
    value.add_argument("--approved-iex-plan-id")
    value.add_argument(
        "--prior-nasdaq-accepted-record-count",
        type=int,
        help="trusted count from the immediately preceding accepted Nasdaq receipt",
    )
    return value


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_source_config(repo_root: Path) -> dict[str, object]:
    return json.loads(
        (repo_root / "config" / "sources.json").read_text(encoding="utf-8")
    )


def _qualification_result(value: object) -> dict[str, object]:
    return {
        "feed": value.feed,
        "state": value.state,
        "reasons": list(value.reasons),
        "snapshot_ids": list(value.snapshot_ids),
        "bar_count": value.bar_count,
        "calendar_release_id": value.calendar_release_id,
        "evidence_state": value.evidence_state,
        "trust_eligible": value.trust_eligible,
    }


def _approved_alpaca_plans(
    *,
    execute_network: bool,
    use_alpaca: bool,
    request_plans: dict[str, NetworkRequestPlan],
    approved_sip_plan_id: str | None,
    approved_iex_plan_id: str | None,
) -> None:
    supplied = {
        "sip": approved_sip_plan_id,
        "iex": approved_iex_plan_id,
    }
    if not execute_network:
        if any(value is not None for value in supplied.values()):
            raise NetworkGuardError(
                "approved Alpaca plan IDs are valid only with --execute-network"
            )
        return
    if not use_alpaca:
        if any(value is not None for value in supplied.values()):
            raise NetworkGuardError(
                "Nasdaq-only execution cannot accept Alpaca plan IDs"
            )
        return
    if any(value is None for value in supplied.values()):
        raise NetworkGuardError(
            "Alpaca execution requires both exact approved request plan IDs"
        )
    expected = {
        "sip": request_plans["alpaca_sip_qualification"].plan_id,
        "iex": request_plans["alpaca_iex_qualification"].plan_id,
    }
    if supplied != expected:
        raise NetworkGuardError("approved Alpaca request plan ID differs")


def main(argv: list[str] | None = None) -> int:
    supplied_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(supplied_argv)
    if not 1 <= args.max_pages <= 100:
        raise NetworkGuardError("Alpaca max pages must be in [1,100]")
    verify_alpaca_pair = args.verify_alpaca_pair is not None
    verify_nasdaq_pair = args.verify_nasdaq_bootstrap_pair is not None
    verify_nasdaq = args.verify_nasdaq_snapshot is not None or verify_nasdaq_pair
    if verify_nasdaq and args.alpaca_only:
        raise NetworkGuardError("Nasdaq snapshot verification cannot select Alpaca")
    if verify_alpaca_pair and args.nasdaq_only:
        raise NetworkGuardError("Alpaca pair verification cannot select Nasdaq")
    if verify_nasdaq_pair and args.prior_nasdaq_accepted_record_count is not None:
        raise NetworkGuardError(
            "Nasdaq bootstrap pair verification does not accept a historical prior count"
        )
    requested_at = datetime.now(timezone.utc)
    symbols = tuple(sorted(set(item.strip().upper() for item in args.symbols.split(",") if item.strip())))
    alpaca_request = AlpacaBarsRequest(symbols, _parse_time(args.start), _parse_time(args.end), requested_at)
    use_alpaca = verify_alpaca_pair or (not args.nasdaq_only and not verify_nasdaq)
    use_nasdaq = not verify_alpaca_pair and not args.alpaca_only
    alpaca_policies = tuple(AlpacaBarsPolicy(feed=feed, asof=None) for feed in ("sip", "iex"))
    repo_root = Path(__file__).resolve().parents[3]
    source_config = _load_source_config(repo_root)
    acquisition_registry = NetworkAcquisitionRegistry.load(
        repo_root / "config" / "network_acquisition_registry.json",
        allowed_root=repo_root / "config",
    )
    trusted_clock = TrustedClock.production()
    request_plans: dict[str, NetworkRequestPlan] = {}
    if use_alpaca and not verify_alpaca_pair:
        for policy in alpaca_policies:
            source = f"alpaca_{policy.feed}_qualification"
            request_plans[source] = NetworkRequestPlan.create(
                registry=acquisition_registry,
                source=source,
                initial_url=alpaca_request.url(policy),
                timeout_seconds=30,
                max_response_bytes=64 * 1024 * 1024,
                max_pages=args.max_pages,
                pagination_parameter="page_token",
            )
    if use_nasdaq:
        request_plans["nasdaqtraded"] = NetworkRequestPlan.create(
            registry=acquisition_registry,
            source="nasdaqtraded",
            initial_url=NASDAQ_TRADED_URL,
            timeout_seconds=30,
            max_response_bytes=MAX_NASDAQ_RESPONSE_BYTES,
            max_pages=1,
            pagination_parameter=None,
        )
    plan: dict[str, object] = {
        "mode": (
            "verify_local_alpaca_pair"
            if verify_alpaca_pair
            else (
                "verify_local_nasdaq_bootstrap_pair"
                if verify_nasdaq_pair
                else (
                    "verify_local_nasdaq_snapshot"
                    if verify_nasdaq
                    else (
                        "network_capture"
                        if args.execute_network
                        else "plan_only"
                    )
                )
            )
        ),
        "alpaca": {
            "requests_without_credentials": [
                {"feed": policy.feed, "url": alpaca_request.url(policy)} for policy in alpaca_policies
            ],
            "purpose": "bounded_delayed_raw_SIP_vs_IEX_entitlement_and_schema_qualification",
        } if use_alpaca else {"selected": False},
        "nasdaq": {"urls": NASDAQ_URLS, "purpose": "as_received_identity_type_snapshot"} if use_nasdaq else {"selected": False},
        "prohibitions": ["model_fit", "alpha_metric", "order_endpoint", "historical_backfill"],
        "request_plans": {
            source: item.as_dict()
            for source, item in sorted(request_plans.items())
        },
    }
    _approved_alpaca_plans(
        execute_network=args.execute_network,
        use_alpaca=use_alpaca,
        request_plans=request_plans,
        approved_sip_plan_id=args.approved_sip_plan_id,
        approved_iex_plan_id=args.approved_iex_plan_id,
    )
    if not args.execute_network and not verify_nasdaq and not verify_alpaca_pair:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if args.execute_network and os.environ.get(AUTH_ENVIRONMENT_TOKEN) != "YES":
        raise NetworkGuardError(f"require {AUTH_ENVIRONMENT_TOKEN}=YES")
    pinned_store_root = Path(str(source_config["snapshot_store_root"]))
    expected_store_root = repo_root / "data" / "vault" / "qualification" / "as_received"
    if pinned_store_root != expected_store_root or source_config.get("project") != "US_stocks_swing_model_v2":
        raise ValueError("qualification snapshot root/project differs from the repository-pinned contract")
    calendar_release: Path | None = None
    expected_calendar_root: Path | None = None
    if use_alpaca:
        calendar_value = source_config.get("qualification_calendar_release")
        if not isinstance(calendar_value, str) or not calendar_value:
            raise NetworkGuardError(
                "Alpaca qualification requires a pinned accepted XNYS calendar release"
            )
        calendar_release = Path(calendar_value)
        expected_calendar_root = repo_root / "data" / "vault" / "accepted"
        try:
            calendar_release.resolve(strict=True).relative_to(
                (expected_calendar_root / "xnys_sessions").resolve(strict=True)
            )
        except (OSError, ValueError) as exc:
            raise NetworkGuardError(
                "qualification calendar is outside the accepted XNYS release root"
            ) from exc
        load_xnys_calendar_release(
            calendar_release,
            accepted_release_root=expected_calendar_root,
        )
    store = AsReceivedSnapshotStore(
        pinned_store_root,
        allowed_root=repo_root,
        acquisition_registry=acquisition_registry,
    )
    if verify_alpaca_pair:
        assert calendar_release is not None and expected_calendar_root is not None
        sip_snapshot = store.load(args.verify_alpaca_pair[0])
        iex_snapshot = store.load(args.verify_alpaca_pair[1])
        if not sip_snapshot.local_integrity_verified or not iex_snapshot.local_integrity_verified:
            raise NetworkGuardError(
                "Alpaca pair requires locally integrity-verified network snapshots"
            )
        offline_requested_at = min(
            sip_snapshot.retrieved_at,
            iex_snapshot.retrieved_at,
        )
        offline_request = AlpacaBarsRequest(
            symbols,
            _parse_time(args.start),
            _parse_time(args.end),
            offline_requested_at,
        )
        qualifications = {
            "sip": qualify_landed_pages(
                offline_request,
                AlpacaBarsPolicy(feed="sip", asof=None),
                (sip_snapshot,),
                calendar_release_directory=calendar_release,
                accepted_release_root=expected_calendar_root,
            ),
            "iex": qualify_landed_pages(
                offline_request,
                AlpacaBarsPolicy(feed="iex", asof=None),
                (iex_snapshot,),
                calendar_release_directory=calendar_release,
                accepted_release_root=expected_calendar_root,
            ),
        }
        eligible = {
            feed: result.eligible for feed, result in qualifications.items()
        }
        selected_feed = (
            "sip"
            if eligible["sip"]
            else "iex"
            if eligible["iex"]
            else None
        )
        selection_reason = (
            "both_pass_prefer_sip"
            if all(eligible.values())
            else "sip_only"
            if eligible["sip"]
            else "iex_only"
            if eligible["iex"]
            else "neither_pass"
        )
        unsigned_assessment = {
            "schema_version": 1,
            "mode": "ALPACA_SIP_IEX_PAIR_ASSESSMENT_NO_WRITES",
            "symbols": list(symbols),
            "start": iso_z(offline_request.start),
            "end": iso_z(offline_request.end),
            "network_registry_id": acquisition_registry.registry_id,
            "snapshots": {
                "sip": {
                    "snapshot_id": sip_snapshot.snapshot_id,
                    "raw_sha256": sip_snapshot.raw_sha256,
                    "retrieved_at": iso_z(sip_snapshot.retrieved_at),
                },
                "iex": {
                    "snapshot_id": iex_snapshot.snapshot_id,
                    "raw_sha256": iex_snapshot.raw_sha256,
                    "retrieved_at": iso_z(iex_snapshot.retrieved_at),
                },
            },
            "qualifications": {
                feed: _qualification_result(result)
                for feed, result in sorted(qualifications.items())
            },
            "selected_feed_candidate": selected_feed,
            "selection_reason": selection_reason,
            "activation_authorized": False,
        }
        assessment = {
            **unsigned_assessment,
            "assessment_id": sha256_bytes(
                canonical_json_bytes(unsigned_assessment)
            ),
        }
        plan["result"] = {"alpaca_pair_assessment": assessment}
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    network_sessions = {}
    if args.execute_network:
        for source, plan_item in request_plans.items():
            network_sessions[source] = start_local_network_execution(
                plan_item,
                registry=acquisition_registry,
                clock=trusted_clock,
            )
    result: dict[str, object] = {}
    if verify_nasdaq_pair:
        snapshot_a_path, snapshot_b_path = args.verify_nasdaq_bootstrap_pair
        snapshot_a = store.load(snapshot_a_path)
        snapshot_b = store.load(snapshot_b_path)
        bootstrap_policy = load_nasdaq_bootstrap_policy(repo_root)
        result["nasdaq_bootstrap"] = verify_nasdaq_bootstrap_pair(
            snapshot_a,
            snapshot_b,
            policy=bootstrap_policy,
        )
        plan["result"] = result
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if verify_nasdaq:
        snapshot = store.load(args.verify_nasdaq_snapshot)
        if not snapshot.local_integrity_verified:
            raise NetworkGuardError(
                "Nasdaq snapshot is not locally integrity verified"
            )
        if snapshot.source != "nasdaqtraded" or snapshot.url != NASDAQ_TRADED_URL:
            raise NetworkGuardError(
                "snapshot is not the exact contracted Nasdaq source"
            )
        records = parse_nasdaq_traded(
            snapshot,
            prior_accepted_record_count=args.prior_nasdaq_accepted_record_count,
        )
        file_created_values = {iso_z(record.file_created_at) for record in records}
        if len(file_created_values) != 1:
            raise ValueError(
                "Nasdaq parse did not preserve one file-creation receipt time"
            )
        result["nasdaq"] = [{
            "url": snapshot.url,
            "snapshot_id": snapshot.snapshot_id,
            "sha256": snapshot.raw_sha256,
            "record_count": len(records),
            "retrieved_at": iso_z(snapshot.retrieved_at),
            "file_created_at": next(iter(file_created_values)),
            "evidence_state": "LOCAL_INTEGRITY_VERIFIED",
            "local_integrity_verified": snapshot.local_integrity_verified,
        }]
        plan["result"] = result
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if use_alpaca:
        assert calendar_release is not None and expected_calendar_root is not None
        feed_results: list[dict[str, object]] = []
        for policy in alpaca_policies:
            landed_pages = guarded_fetch_landed_pages(
                alpaca_request,
                snapshot_store=store,
                api_key_id=os.environ.get("APCA_API_KEY_ID", ""),
                api_secret_key=os.environ.get("APCA_API_SECRET_KEY", ""),
                policy=policy,
                network_enabled=True,
                max_pages=args.max_pages,
                clock=trusted_clock,
                authorization_session=network_sessions[
                    f"alpaca_{policy.feed}_qualification"
                ],
            )
            qualification = qualify_landed_pages(
                alpaca_request,
                policy,
                landed_pages,
                calendar_release_directory=calendar_release,
                accepted_release_root=expected_calendar_root,
            )
            feed_results.append(_qualification_result(qualification))
        result["alpaca_feed_qualification"] = feed_results
        result["qualified_feed_candidates"] = [
            row["feed"]
            for row in feed_results
            if row["state"] == "PASS" and row["trust_eligible"] is True
        ]
    nasdaq_results: list[dict[str, object]] = []
    for url in NASDAQ_URLS if use_nasdaq else ():
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "www.nasdaqtrader.com":
            raise NetworkGuardError("Nasdaq URL is outside the frozen host")
        request_attempt = assert_local_network_request(
            network_sessions["nasdaqtraded"],
            source="nasdaqtraded",
            url=url,
            timeout_seconds=30,
            max_response_bytes=MAX_NASDAQ_RESPONSE_BYTES,
            page_index=0,
            expected_page_token=None,
            clock=trusted_clock,
        )
        with open_without_redirects(
            Request(url, method="GET"), timeout_seconds=30
        ) as response:
            raw = response.read(MAX_NASDAQ_RESPONSE_BYTES + 1)
            headers = normalize_response_headers(
                {
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in ALLOWED_RESPONSE_HEADERS
                }
            )
            http_status = int(response.status)
            response_url = str(response.geturl())
        if response_url != url:
            raise NetworkGuardError("Nasdaq response redirected away from the exact approved URL")
        if len(raw) > MAX_NASDAQ_RESPONSE_BYTES:
            raise ValueError("Nasdaq response exceeded the bounded byte limit")
        transport_evidence = _bind_network_response(
            request_attempt,
            requested_url=url,
            response_url=response_url,
            http_status=http_status,
            raw=raw,
            headers=headers,
        )
        snapshot = store._land_network_response(
            transport_evidence=transport_evidence,
            source="nasdaqtraded",
            requested_url=url,
            response_url=response_url,
            http_status=http_status,
            raw=raw,
            headers=headers,
            clock=trusted_clock,
            max_bytes=MAX_NASDAQ_RESPONSE_BYTES,
        )
        nasdaq_results.append(
            {
                "url": url,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_directory": str(snapshot.root),
                "sha256": sha256_bytes(raw),
                "retrieved_at": iso_z(snapshot.retrieved_at),
                "evidence_state": "LOCAL_INTEGRITY_VERIFIED",
                "local_integrity_verified": snapshot.local_integrity_verified,
            }
        )
    if use_nasdaq:
        result["nasdaq"] = nasdaq_results
    plan["result"] = result
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
