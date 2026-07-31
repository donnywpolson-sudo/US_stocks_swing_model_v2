from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.errors import (
    ContractError,
    IntegrityError,
    NetworkGuardError,
)
import us_stocks_swing_model_v2.providers.corporate_actions as corporate_actions_module
from us_stocks_swing_model_v2.providers.corporate_actions import (
    CORPORATE_ACTIONS_ENDPOINT,
    PROCESS_DATE_ACQUISITION_COVERAGE,
    CorporateActionEvidence,
    CorporateActionCoverageEvidence,
    CorporateActionsRequest,
    parse_landed_corporate_actions,
)
from us_stocks_swing_model_v2.providers.snapshots import AsReceivedSnapshotStore


def _snapshot_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="corporate-action-pages",
        scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
    )


def _url(request: CorporateActionsRequest, token: str | None = None) -> str:
    params = request.parameters()
    if token:
        params["page_token"] = token
    return f"{CORPORATE_ACTIONS_ENDPOINT}?{urlencode(params)}"


def test_corporate_action_transport_rejects_redirect_before_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 31),
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        ("ABC",),
    )

    def reject(*args, **kwargs):
        raise NetworkGuardError("credentialed provider redirect rejected before retransmission")

    monkeypatch.setattr(
        corporate_actions_module, "open_without_redirects", reject
    )
    with pytest.raises(NetworkGuardError, match="before retransmission"):
        corporate_actions_module._fetch_page(
            request,
            api_key_id="id",
            api_secret_key="secret",
            timeout_seconds=30,
            clock=TrustedClock.production(),
            request_attempt=object(),  # type: ignore[arg-type]
        )


def test_corporate_action_request_enforces_trusted_time_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    current = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 31),
        trusted - timedelta(minutes=15),
        ("ABC",),
    )
    current.validate_against_trusted_time(trusted)
    with pytest.raises(ContractError, match="cannot follow trusted acquisition date"):
        replace(current, end=date(2026, 8, 1)).validate_against_trusted_time(
            trusted
        )

    future = replace(current, requested_at=trusted + timedelta(microseconds=1))
    with pytest.raises(ContractError, match="later than trusted"):
        future.validate_against_trusted_time(trusted)

    stale = replace(
        current,
        requested_at=trusted - timedelta(minutes=15, microseconds=1),
    )
    with pytest.raises(ContractError, match="stale relative"):
        stale.validate_against_trusted_time(trusted)
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    clock = TrustedClock.synthetic_fixed(
        trusted,
        permit=SyntheticOnlyPermit.create(
            fixture_id="corporate-action-trusted-time",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )
    with pytest.raises(ContractError, match="stale relative"):
        corporate_actions_module.guarded_fetch_corporate_action_pages(
            stale,
            snapshot_store=object(),  # type: ignore[arg-type]
            api_key_id="fixture-key",
            api_secret_key="fixture-secret",
            network_enabled=True,
            clock=clock,
        )


def test_guarded_fetch_persists_the_approved_request_plan_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(date(2026, 7, 1), date(2026, 7, 31), requested, ("ABC",))
    observed: dict[str, object] = {}
    page = type("Page", (), {"read_verified_bytes": lambda self: b'{"corporate_actions":{},"next_page_token":null}'})()
    session = type("Session", (), {"plan": type("Plan", (), {"plan_id": "a" * 64})()})()
    evidence = type("Evidence", (), {
        "transport_evidence": object(), "url": _url(request), "response_url": _url(request),
        "status": 200, "raw_bytes": b"{}", "headers": {"content-type": "application/json"},
    })()
    store = type("Store", (), {
        "_land_network_response": lambda self, **kwargs: observed.update(kwargs) or page,
    })()
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    monkeypatch.setattr(corporate_actions_module, "assert_local_network_request", lambda *args, **kwargs: object())
    monkeypatch.setattr(corporate_actions_module, "_fetch_page", lambda *args, **kwargs: evidence)
    clock = TrustedClock.synthetic_fixed(
        requested,
        permit=SyntheticOnlyPermit.create(fixture_id="corporate-action-binding", scope="TRUSTED_CLOCK_FIXED_TIME"),
    )

    pages = corporate_actions_module.guarded_fetch_corporate_action_pages(
        request,
        snapshot_store=store,
        api_key_id="fixture-key",
        api_secret_key="fixture-secret",
        network_enabled=True,
        max_pages=1,
        clock=clock,
        authorization_session=session,
    )
    assert pages == (page,)
    assert observed["requested_at"] == requested
    assert observed["request_plan_id"] == "a" * 64


def test_corporate_action_pages_are_parsed_only_from_landed_receipt_time(tmp_path) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(date(2026, 7, 1), date(2026, 7, 15), requested, ("ABC",))
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    first = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {
                "corporate_actions": {
                    "forward_splits": [
                        {
                            "id": "action-1",
                            "symbol": "ABC",
                            "process_date": "2026-07-10",
                            "ex_date": "2026-07-08",
                            "effective_date": "2026-07-08",
                            "old_rate": 1.0,
                            "new_rate": 2.0,
                        }
                    ]
                },
                "next_page_token": "page-2",
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )
    second = store.land(
        source="alpaca_corporate_actions",
        url=_url(request, "page-2"),
        http_status=200,
        raw=json.dumps({"corporate_actions": {}, "next_page_token": None}).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 2, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )
    result = parse_landed_corporate_actions(request, (first, second))
    evidence = result.actions
    assert len(evidence) == 1
    assert evidence[0].provider_process_date == date(2026, 7, 10)
    assert evidence[0].effective_session == date(2026, 7, 8)
    assert evidence[0].symbol == "ABC"
    assert evidence[0].involved_symbols == ("ABC",)
    assert evidence[0].received_at == first.retrieved_at
    assert evidence[0].source_epoch == "alpaca_corporate_actions_v1"
    assert len(evidence[0].snapshot_id) == 64
    assert len(evidence[0].raw_row_sha256) == 64
    assert evidence[0].acquisition_mode == "SYNTHETIC_DIRECT_NOT_AS_RECEIVED"
    assert evidence[0].evidence_state == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
    assert evidence[0].acquisition_capability_id == first.acquisition_capability_id
    assert evidence[0].synthetic_permit_ids == (first.synthetic_permit_id,)
    assert evidence[0].provider_process_date_is_causal is False
    assert CorporateActionEvidence.from_dict(evidence[0].as_dict()) == evidence[0]
    assert result.coverage.requested_symbols == ("ABC",)
    assert result.coverage.snapshot_ids == (first.snapshot_id, second.snapshot_id)
    assert result.coverage.process_date_start == request.start
    assert result.coverage.process_date_end == request.end
    with pytest.raises(ContractError, match="beyond acquisition date"):
        replace(
            result.coverage,
            process_date_end=date(2026, 7, 16),
        ).validate()
    assert (
        result.coverage.coverage_semantics
        == PROCESS_DATE_ACQUISITION_COVERAGE
    )
    assert result.coverage.acquisition_mode == "SYNTHETIC_DIRECT_NOT_AS_RECEIVED"
    assert result.coverage.evidence_state == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
    assert len(result.coverage.coverage_id) == 64
    result.coverage.validate()
    assert CorporateActionCoverageEvidence(**{
        "schema_version": result.coverage.schema_version,
        "coverage_semantics": result.coverage.coverage_semantics,
        "process_date_start": result.coverage.process_date_start,
        "process_date_end": result.coverage.process_date_end,
        "requested_at": result.coverage.requested_at,
        "requested_symbols": result.coverage.requested_symbols,
        "completed_at": result.coverage.completed_at,
        "snapshot_ids": result.coverage.snapshot_ids,
        "acquisition_mode": result.coverage.acquisition_mode,
        "evidence_state": result.coverage.evidence_state,
        "acquisition_capability_ids": result.coverage.acquisition_capability_ids,
        "synthetic_permit_ids": result.coverage.synthetic_permit_ids,
        "source_epoch": result.coverage.source_epoch,
        "coverage_id": result.coverage.coverage_id,
    }).as_dict() == result.coverage.as_dict()
    with pytest.raises(ContractError, match="binding is inconsistent"):
        replace(evidence[0], evidence_state="NETWORK_AS_RECEIVED").validate()
    with pytest.raises(ContractError, match="exact JSON arrays"):
        CorporateActionEvidence.from_dict(
            {**evidence[0].as_dict(), "synthetic_permit_ids": evidence[0].synthetic_permit_ids}
        )
    with pytest.raises(ContractError, match="exact JSON arrays"):
        CorporateActionEvidence.from_dict(
            {**evidence[0].as_dict(), "involved_symbols": ("ABC",)}
        )

    first.raw_path.write_bytes(b"tampered-after-land")
    with pytest.raises(IntegrityError, match="raw bytes"):
        parse_landed_corporate_actions(request, (first, second))


def test_unfiltered_corporate_action_without_asset_attribution_fails_closed(
    tmp_path,
) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 15),
        requested,
        (),
    )
    store = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
    )
    page = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {
                "corporate_actions": {
                    "cash_dividends": [
                        {
                            "id": "assetless-action",
                            "process_date": "2026-07-10",
                            "ex_date": "2026-07-08",
                        }
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=requested + timedelta(minutes=1),
        synthetic_permit=_snapshot_permit(),
    )
    with pytest.raises(
        ContractError,
        match="involved symbols are not exact canonical evidence",
    ):
        parse_landed_corporate_actions(request, (page,))


@pytest.mark.parametrize(
    ("ex_date", "effective_date", "message"),
    [
        ("2026-07-08", "2026-07-09", "ex_date and effective_date conflict"),
        ("20260708", "2026-07-08", "ex_date must be canonical ISO date text"),
    ],
)
def test_corporate_action_ambiguous_effective_dates_fail_closed(
    tmp_path,
    ex_date: str,
    effective_date: str,
    message: str,
) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 15),
        requested,
        ("ABC",),
    )
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    page = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {
                "corporate_actions": {
                    "forward_splits": [
                        {
                            "id": "conflicting-dates",
                            "symbol": "ABC",
                            "process_date": "2026-07-10",
                            "ex_date": ex_date,
                            "effective_date": effective_date,
                            "old_rate": 1.0,
                            "new_rate": 2.0,
                        }
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )

    with pytest.raises(ContractError, match=message):
        parse_landed_corporate_actions(request, (page,))


def test_complete_empty_corporate_action_response_yields_governed_absence(
    tmp_path,
) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 15),
        requested,
        ("ABC",),
    )
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    empty = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {"corporate_actions": {}, "next_page_token": None}
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )

    result = parse_landed_corporate_actions(request, (empty,))
    assert result.actions == ()
    assert result.coverage.requested_symbols == ("ABC",)
    assert result.coverage.snapshot_ids == (empty.snapshot_id,)
    assert result.coverage.completed_at == empty.retrieved_at
    result.coverage.validate()


def test_process_date_acquisition_does_not_claim_effective_event_completeness(
    tmp_path,
) -> None:
    requested = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 31),
        requested,
        ("ABC",),
    )
    store = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
    )
    page = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {
                "corporate_actions": {
                    "forward_splits": [
                        {
                            "id": "cross-boundary",
                            "symbol": "ABC",
                            "process_date": "2026-07-10",
                            "ex_date": "2026-08-05",
                            "old_rate": 1.0,
                            "new_rate": 2.0,
                        }
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 31, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )

    result = parse_landed_corporate_actions(request, (page,))
    assert result.actions[0].provider_process_date == date(2026, 7, 10)
    assert result.actions[0].effective_session == date(2026, 8, 5)
    assert (
        result.coverage.coverage_semantics
        == PROCESS_DATE_ACQUISITION_COVERAGE
    )
    assert result.coverage.process_date_end == date(2026, 7, 31)


def test_payable_date_alone_does_not_claim_effective_session(tmp_path) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 15),
        requested,
        ("ABC",),
    )
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    page = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {
                "corporate_actions": {
                    "cash_dividends": [
                        {
                            "id": "payable-only",
                            "symbol": "ABC",
                            "process_date": "2026-07-10",
                            "payable_date": "2026-07-20",
                        }
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )

    result = parse_landed_corporate_actions(request, (page,))
    assert result.actions[0].effective_session is None


def test_multi_party_action_preserves_every_involved_symbol(
    tmp_path,
) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 15),
        requested,
        ("ABC",),
    )
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    page = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {
                "corporate_actions": {
                    "cash_mergers": [
                        {
                            "id": "two-party-merger",
                            "symbol": "ABC",
                            "acquiree_symbol": "DEF",
                            "process_date": "2026-07-10",
                            "effective_date": "2026-07-20",
                        }
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )

    result = parse_landed_corporate_actions(request, (page,))
    assert len(result.actions) == 1
    assert result.actions[0].symbol == "ABC"
    assert result.actions[0].involved_symbols == ("ABC", "DEF")
    assert CorporateActionEvidence.from_dict(
        result.actions[0].as_dict()
    ) == result.actions[0]


@pytest.mark.parametrize("symbol", ["abc", " ABC ", "AbC"])
def test_corporate_action_symbols_require_exact_canonical_wire_text(
    symbol: str,
) -> None:
    with pytest.raises(ContractError, match="exact canonical text"):
        corporate_actions_module._symbols(
            {
                "symbol": symbol,
                "acquiree_symbol": "DEF",
            }
        )


def test_symbol_coverage_is_bound_to_request_not_event_occurrence(tmp_path) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 15),
        requested,
        ("ABC", "DEF"),
    )
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    partial = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {
                "corporate_actions": {
                    "forward_splits": [
                        {
                            "id": "action-1",
                            "symbol": "ABC",
                            "process_date": "2026-07-10",
                            "ex_date": "2026-07-08",
                            "old_rate": 1.0,
                            "new_rate": 2.0,
                        }
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )

    result = parse_landed_corporate_actions(request, (partial,))
    assert tuple(action.symbol for action in result.actions) == ("ABC",)
    assert result.coverage.requested_symbols == ("ABC", "DEF")


def test_unrequested_symbol_is_rejected(tmp_path) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 15),
        requested,
        ("ABC",),
    )
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    page = store.land(
        source="alpaca_corporate_actions",
        url=_url(request),
        http_status=200,
        raw=json.dumps(
            {
                "corporate_actions": {
                    "forward_splits": [
                        {
                            "id": "action-1",
                            "symbol": "DEF",
                            "process_date": "2026-07-10",
                            "ex_date": "2026-07-08",
                            "old_rate": 1.0,
                            "new_rate": 2.0,
                        }
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )

    with pytest.raises(ContractError, match="unrequested symbol"):
        parse_landed_corporate_actions(request, (page,))


def test_page_scope_change_is_rejected(tmp_path) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 15),
        requested,
        ("ABC",),
    )
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    altered = CorporateActionsRequest(
        date(2026, 7, 2),
        date(2026, 7, 15),
        requested,
        ("ABC",),
    )
    page = store.land(
        source="alpaca_corporate_actions",
        url=_url(altered),
        http_status=200,
        raw=json.dumps(
            {"corporate_actions": {}, "next_page_token": None}
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 7, 15, 12, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )

    with pytest.raises(ContractError, match="request scope differs"):
        parse_landed_corporate_actions(request, (page,))
