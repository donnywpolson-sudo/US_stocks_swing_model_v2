from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request

from ..common import atomic_write, canonical_json_bytes, iso_z, sha256_bytes
from ..clock import TrustedClock
from ..errors import NetworkGuardError
from ..exchange_calendar import load_xnys_calendar_release
from ..governance import (
    load_external_authority,
    load_signed_authorization_receipt,
)
from ..providers.alpaca import (
    AUTH_ENVIRONMENT_TOKEN,
    AlpacaBarsPolicy,
    AlpacaBarsRequest,
    guarded_fetch_landed_pages,
    qualify_landed_pages,
)
from ..providers.nasdaq import NASDAQ_TRADED_URL, parse_nasdaq_traded
from ..providers.http import open_without_redirects
from ..providers.network_authorization import (
    NetworkAuthorizationUseStore,
    NetworkRequestPlan,
    assert_authorized_network_request,
    network_authorization_request,
)
from ..providers.snapshots import (
    ALLOWED_RESPONSE_HEADERS,
    AsReceivedSnapshotStore,
    NetworkAcquisitionRegistry,
    NETWORK_ACQUISITION_ATTESTATION_SCOPE,
    network_acquisition_attestation_bindings,
    normalize_response_headers,
)


NASDAQ_URLS = (NASDAQ_TRADED_URL,)
MAX_NASDAQ_RESPONSE_BYTES = 32 * 1024 * 1024


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Plan or run bounded free-source qualification")
    value.add_argument("--plan-only", action="store_true", help="documentary flag; this is the default")
    mode = value.add_mutually_exclusive_group()
    mode.add_argument("--execute-network", action="store_true", help=f"also requires {AUTH_ENVIRONMENT_TOKEN}=YES")
    mode.add_argument(
        "--verify-nasdaq-snapshot",
        type=Path,
        help="verify one already captured Nasdaq snapshot with a detached attestation",
    )
    selection = value.add_mutually_exclusive_group()
    selection.add_argument("--nasdaq-only", action="store_true")
    selection.add_argument("--alpaca-only", action="store_true")
    value.add_argument("--symbols", default="AAPL,SPY")
    value.add_argument("--start", default="2024-01-02T00:00:00Z")
    value.add_argument("--end", default="2024-01-10T00:00:00Z")
    value.add_argument("--acquisition-attestation", type=Path)
    value.add_argument("--attestation-authority-registry", type=Path)
    value.add_argument("--attestation-key-id")
    value.add_argument("--attestation-public-key-file", type=Path)
    value.add_argument("--network-authorization", action="append", type=Path, default=[])
    value.add_argument("--network-authority-registry", type=Path)
    value.add_argument("--network-key-id")
    value.add_argument("--network-public-key-file", type=Path)
    value.add_argument("--authorization-request-directory", type=Path)
    return value


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    verify_nasdaq = args.verify_nasdaq_snapshot is not None
    if verify_nasdaq and args.alpaca_only:
        raise NetworkGuardError("Nasdaq snapshot verification cannot select Alpaca")
    requested_at = datetime.now(timezone.utc)
    symbols = tuple(sorted(set(item.strip().upper() for item in args.symbols.split(",") if item.strip())))
    alpaca_request = AlpacaBarsRequest(symbols, _parse_time(args.start), _parse_time(args.end), requested_at)
    use_alpaca = not args.nasdaq_only and not verify_nasdaq
    use_nasdaq = not args.alpaca_only
    alpaca_policies = tuple(AlpacaBarsPolicy(feed=feed, asof=None) for feed in ("sip", "iex"))
    repo_root = Path(__file__).resolve().parents[3]
    source_config = json.loads((repo_root / "config" / "sources.json").read_text(encoding="utf-8"))
    acquisition_registry = NetworkAcquisitionRegistry.load(
        repo_root / "config" / "network_acquisition_registry.json"
    )
    trusted_clock = TrustedClock.production()
    request_plans: dict[str, NetworkRequestPlan] = {}
    if use_alpaca:
        for policy in alpaca_policies:
            source = f"alpaca_{policy.feed}_qualification"
            request_plans[source] = NetworkRequestPlan.create(
                registry=acquisition_registry,
                source=source,
                initial_url=alpaca_request.url(policy),
                timeout_seconds=30,
                max_response_bytes=64 * 1024 * 1024,
                max_pages=10,
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
    authorization_requests = {
        source: network_authorization_request(plan_item, clock=trusted_clock)
        for source, plan_item in sorted(request_plans.items())
    }
    plan: dict[str, object] = {
        "mode": (
            "verify_attested_nasdaq_snapshot"
            if verify_nasdaq
            else ("network_capture" if args.execute_network else "plan_only")
        ),
        "alpaca": {
            "requests_without_credentials": [
                {"feed": policy.feed, "url": alpaca_request.url(policy)} for policy in alpaca_policies
            ],
            "purpose": "bounded_delayed_raw_SIP_vs_IEX_entitlement_and_schema_qualification",
        } if use_alpaca else {"selected": False},
        "nasdaq": {"urls": NASDAQ_URLS, "purpose": "as_received_identity_type_snapshot"} if use_nasdaq else {"selected": False},
        "prohibitions": ["model_fit", "alpha_metric", "order_endpoint", "historical_backfill"],
        "authorization_requests": authorization_requests,
    }
    network_authority_inputs = (
        args.network_authorization,
        args.network_authority_registry,
        args.network_key_id,
        args.network_public_key_file,
    )
    if not args.execute_network and not verify_nasdaq:
        if any(network_authority_inputs):
            raise NetworkGuardError(
                "network authorization inputs are accepted only with --execute-network"
            )
        if args.authorization_request_directory is not None:
            destination = args.authorization_request_directory
            if not destination.is_absolute() or destination.exists():
                raise NetworkGuardError(
                    "authorization request directory must be a new absolute path"
                )
            destination.mkdir(parents=True)
            for source, request_payload in authorization_requests.items():
                atomic_write(
                    destination / f"{source}.json",
                    canonical_json_bytes(request_payload),
                )
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if args.authorization_request_directory is not None:
        raise NetworkGuardError(
            "authorization request output is available only in plan-only mode"
        )
    if args.execute_network and os.environ.get(AUTH_ENVIRONMENT_TOKEN) != "YES":
        raise NetworkGuardError(f"require {AUTH_ENVIRONMENT_TOKEN}=YES")
    if verify_nasdaq and any(network_authority_inputs):
        raise NetworkGuardError(
            "network authorization inputs cannot be used for offline verification"
        )
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
    network_sessions = {}
    if args.execute_network:
        missing_network_authority = [
            name
            for name, value in {
                "--network-authorization": args.network_authorization,
                "--network-authority-registry": args.network_authority_registry,
                "--network-key-id": args.network_key_id,
                "--network-public-key-file": args.network_public_key_file,
            }.items()
            if not value
        ]
        if missing_network_authority:
            raise NetworkGuardError(
                "network execution requires: " + ", ".join(missing_network_authority)
            )
        authority = load_external_authority(
            args.network_authority_registry,
            key_id=args.network_key_id,
            verification_key=args.network_public_key_file.read_bytes(),
        )
        receipts = [
            load_signed_authorization_receipt(path)
            for path in args.network_authorization
        ]
        by_subject = {receipt.subject_id: receipt for receipt in receipts}
        if len(by_subject) != len(receipts) or set(by_subject) != {
            item.plan_id for item in request_plans.values()
        }:
            raise NetworkGuardError(
                "network authorization receipts must exactly cover selected request plans"
            )
        use_store = NetworkAuthorizationUseStore(
            repo_root / "data" / "vault" / "qualification" / "network_authorization_uses",
            allowed_root=repo_root,
        )
        for source, plan_item in request_plans.items():
            network_sessions[source] = use_store.authorize(
                plan=plan_item,
                receipt=by_subject[plan_item.plan_id],
                authority=authority,
                clock=trusted_clock,
            )
    result: dict[str, object] = {}
    if verify_nasdaq:
        required_attestation = {
            "--acquisition-attestation": args.acquisition_attestation,
            "--attestation-authority-registry": args.attestation_authority_registry,
            "--attestation-key-id": args.attestation_key_id,
            "--attestation-public-key-file": args.attestation_public_key_file,
        }
        missing = [
            name for name, value in required_attestation.items() if value is None
        ]
        if missing:
            raise NetworkGuardError(
                "attested Nasdaq verification requires: " + ", ".join(missing)
            )
        authority = load_external_authority(
            args.attestation_authority_registry,
            key_id=args.attestation_key_id,
            verification_key=args.attestation_public_key_file.read_bytes(),
        )
        snapshot = store.load_attested(
            args.verify_nasdaq_snapshot,
            attestation_path=args.acquisition_attestation,
            authority=authority,
            clock=trusted_clock,
        )
        if snapshot.source != "nasdaqtraded" or snapshot.url != NASDAQ_TRADED_URL:
            raise NetworkGuardError(
                "attested snapshot is not the exact contracted Nasdaq source"
            )
        records = parse_nasdaq_traded(snapshot)
        file_created_values = {iso_z(record.file_created_at) for record in records}
        if len(file_created_values) != 1:
            raise ValueError(
                "Nasdaq parse did not preserve one file-creation receipt time"
            )
        result["nasdaq"] = [{
            "url": snapshot.url,
            "snapshot_id": snapshot.snapshot_id,
            "attestation_receipt_id": (
                snapshot.acquisition_attestation.receipt_id
                if snapshot.acquisition_attestation is not None
                else None
            ),
            "sha256": snapshot.raw_sha256,
            "record_count": len(records),
            "retrieved_at": iso_z(snapshot.retrieved_at),
            "file_created_at": next(iter(file_created_values)),
            "trust_eligible": snapshot.trust_eligible,
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
            feed_results.append(
                {
                    "feed": policy.feed,
                    "state": qualification.state,
                    "reasons": list(qualification.reasons),
                    "snapshot_ids": list(qualification.snapshot_ids),
                    "bar_count": qualification.bar_count,
                    "calendar_release_id": qualification.calendar_release_id,
                    "evidence_state": qualification.evidence_state,
                    "trust_eligible": qualification.trust_eligible,
                }
            )
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
        assert_authorized_network_request(
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
        snapshot = store._land_network_response(
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
                "trust_eligible": snapshot.trust_eligible,
                "attestation_request": {
                    "schema_version": 1,
                    "scope": NETWORK_ACQUISITION_ATTESTATION_SCOPE,
                    "snapshot_id": snapshot.snapshot_id,
                    "bindings": network_acquisition_attestation_bindings(snapshot),
                },
            }
        )
    if use_nasdaq:
        result["nasdaq"] = nasdaq_results
    plan["result"] = result
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
