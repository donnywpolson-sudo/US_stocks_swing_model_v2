from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
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
    CorporateActionEvidence,
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
        )


def test_corporate_action_pages_are_parsed_only_from_landed_receipt_time(tmp_path) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(date(2026, 7, 1), date(2026, 7, 31), requested, ("ABC",))
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
    evidence = parse_landed_corporate_actions(request, (first, second))
    assert len(evidence) == 1
    assert evidence[0].provider_process_date == date(2026, 7, 10)
    assert evidence[0].effective_session == date(2026, 7, 8)
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
    with pytest.raises(ContractError, match="binding is inconsistent"):
        replace(evidence[0], evidence_state="NETWORK_AS_RECEIVED").validate()
    with pytest.raises(ContractError, match="exact JSON array"):
        CorporateActionEvidence.from_dict(
            {**evidence[0].as_dict(), "synthetic_permit_ids": evidence[0].synthetic_permit_ids}
        )

    first.raw_path.write_bytes(b"tampered-after-land")
    with pytest.raises(IntegrityError, match="raw bytes"):
        parse_landed_corporate_actions(request, (first, second))


def test_empty_corporate_action_response_is_unresolved_absence(tmp_path) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 31),
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

    with pytest.raises(ContractError, match="unresolved absence evidence"):
        parse_landed_corporate_actions(request, (empty,))


def test_partial_requested_symbol_coverage_is_rejected(tmp_path) -> None:
    requested = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    request = CorporateActionsRequest(
        date(2026, 7, 1),
        date(2026, 7, 31),
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

    with pytest.raises(ContractError, match="requested-symbol coverage is incomplete: DEF"):
        parse_landed_corporate_actions(request, (partial,))
