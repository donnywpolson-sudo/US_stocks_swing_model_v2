from __future__ import annotations

import json
from datetime import date, datetime, timezone
from urllib.parse import urlencode

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.errors import IntegrityError
from us_stocks_swing_model_v2.providers.corporate_actions import (
    CORPORATE_ACTIONS_ENDPOINT,
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
    assert evidence[0].provider_process_date_is_causal is False

    first.raw_path.write_bytes(b"tampered-after-land")
    with pytest.raises(IntegrityError, match="raw bytes"):
        parse_landed_corporate_actions(request, (first, second))
