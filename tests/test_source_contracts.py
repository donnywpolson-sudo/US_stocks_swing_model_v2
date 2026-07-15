from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.errors import ContractError, NetworkGuardError
from us_stocks_swing_model_v2.cli.qualify_free_sources import main as qualification_main
import us_stocks_swing_model_v2.cli.qualify_free_sources as qualification_cli
from us_stocks_swing_model_v2.providers.alpaca import (
    AlpacaBarsPolicy,
    AlpacaBarsRequest,
    guarded_fetch_json,
)
import us_stocks_swing_model_v2.providers.alpaca as alpaca_module
from us_stocks_swing_model_v2.providers.nasdaq import (
    NASDAQ_TRADED_URL,
    NasdaqCompletenessPolicy,
    parse_nasdaq_traded,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    AsReceivedSnapshotStore,
    NetworkAcquisitionRegistry,
)
from us_stocks_swing_model_v2.schemas import SecurityType


def _nasdaq_policy() -> NasdaqCompletenessPolicy:
    return NasdaqCompletenessPolicy.synthetic_fixture(
        permit=SyntheticOnlyPermit.create(
            fixture_id="source-contracts",
            scope="NASDAQ_COMPLETENESS_FIXTURE",
        )
    )


def _snapshot_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="source-contract-snapshots",
        scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
    )


def test_alpaca_policy_requires_explicit_feed_and_omits_default_asof() -> None:
    request = AlpacaBarsRequest(
        symbols=("AAPL", "SPY"),
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 10, tzinfo=timezone.utc),
        requested_at=datetime(2024, 1, 11, tzinfo=timezone.utc),
    )
    params = request.parameters(AlpacaBarsPolicy(feed="iex", asof=None))
    assert params["feed"] == "iex"
    assert params["timeframe"] == "1Day"
    assert params["adjustment"] == "raw"
    assert "asof" not in params
    assert params["sort"] == "asc"


def test_alpaca_policy_rejects_recent_end_or_feed_drift() -> None:
    recent = AlpacaBarsRequest(
        ("SPY",),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, 0, 50, tzinfo=timezone.utc),
        datetime(2024, 1, 2, 1, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(ContractError, match="too recent"):
        recent.parameters(AlpacaBarsPolicy(feed="sip"))
    with pytest.raises(ContractError, match="explicitly qualified"):
        AlpacaBarsPolicy().validate()
    deliberate = AlpacaBarsPolicy(feed="sip", asof="2024-01-01")
    deliberate.validate()
    with pytest.raises(ContractError, match="ISO date"):
        AlpacaBarsPolicy(feed="sip", asof="-").validate()


def test_network_fetch_is_disabled_without_dual_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREE_SOURCE_QUALIFICATION_APPROVED", raising=False)
    request = AlpacaBarsRequest(
        ("SPY",),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        datetime(2024, 1, 3, tzinfo=timezone.utc),
    )
    with pytest.raises(NetworkGuardError):
        guarded_fetch_json(
            request,
            api_key_id="x",
            api_secret_key="y",
            policy=AlpacaBarsPolicy(feed="iex"),
            network_enabled=False,
        )


def test_qualification_cli_is_no_network_by_default_and_requires_dual_authorization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("FREE_SOURCE_QUALIFICATION_APPROVED", raising=False)

    def unexpected_network(*args, **kwargs):
        raise AssertionError("plan-only qualification attempted network access")

    monkeypatch.setattr(qualification_cli, "urlopen", unexpected_network)
    assert qualification_main([]) == 0
    assert '"mode": "plan_only"' in capsys.readouterr().out
    with pytest.raises(NetworkGuardError, match="FREE_SOURCE_QUALIFICATION_APPROVED"):
        qualification_main(["--execute-network"])


def test_nasdaq_only_qualification_does_not_require_an_alpaca_calendar(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Response:
        status = 200
        headers = {"content-type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit: int) -> bytes:
            return b"bounded-nasdaq-fixture"

        def geturl(self) -> str:
            return NASDAQ_TRADED_URL

    class Snapshot:
        retrieved_at = datetime(2026, 7, 15, tzinfo=timezone.utc)

    class Store:
        def __init__(self, *args, **kwargs):
            pass

        def _land_network_response(self, **kwargs):
            return Snapshot()

    class Record:
        file_created_at = datetime(2026, 7, 14, 22, tzinfo=timezone.utc)

    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    monkeypatch.setattr(qualification_cli, "urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setattr(qualification_cli, "AsReceivedSnapshotStore", Store)
    monkeypatch.setattr(qualification_cli, "parse_nasdaq_traded", lambda snapshot: (Record(),))
    assert qualification_main(["--execute-network", "--nasdaq-only"]) == 0
    output = capsys.readouterr().out
    assert '"record_count": 1' in output
    assert '"nasdaq"' in output


def test_alpaca_response_is_bounded_and_retrieval_time_is_post_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class Response:
        status = 200
        headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit: int) -> bytes:
            observed["read_finished"] = datetime.now(timezone.utc)
            return b'{"bars":{}}'

        def geturl(self) -> str:
            return str(observed["request_url"])

    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    def open_response(http_request, *args, **kwargs):
        observed["request_url"] = http_request.full_url
        return Response()

    monkeypatch.setattr(alpaca_module, "urlopen", open_response)
    request = AlpacaBarsRequest(
        ("SPY",),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        datetime(2024, 1, 3, tzinfo=timezone.utc),
    )
    evidence = guarded_fetch_json(
        request,
        api_key_id="id",
        api_secret_key="secret",
        policy=AlpacaBarsPolicy(feed="iex", asof=None),
        network_enabled=True,
    )
    assert evidence.retrieved_at >= observed["read_finished"]
    assert "feed=iex" in evidence.url
    assert "asof=" not in evidence.url

    class Redirected(Response):
        def geturl(self) -> str:
            return "https://example.invalid/redirected"

    monkeypatch.setattr(alpaca_module, "urlopen", lambda *args, **kwargs: Redirected())
    with pytest.raises(ContractError, match="redirected"):
        guarded_fetch_json(
            request,
            api_key_id="id",
            api_secret_key="secret",
            policy=AlpacaBarsPolicy(feed="iex"),
            network_enabled=True,
        )

    monkeypatch.setattr(alpaca_module, "MAX_ALPACA_RESPONSE_BYTES", 4)

    class Oversized(Response):
        def read(self, limit: int) -> bytes:
            return b"12345"

    def open_oversized(http_request, *args, **kwargs):
        observed["request_url"] = http_request.full_url
        return Oversized()

    monkeypatch.setattr(alpaca_module, "urlopen", open_oversized)
    with pytest.raises(ContractError, match="bounded byte"):
        guarded_fetch_json(
            request,
            api_key_id="id",
            api_secret_key="secret",
            policy=AlpacaBarsPolicy(feed="sip"),
            network_enabled=True,
        )


def test_snapshot_store_rejects_empty_or_over_bound_responses(tmp_path) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    with pytest.raises(ContractError, match="empty, oversized"):
        store.land(
            source="fixture",
            url="https://example.invalid",
            http_status=200,
            raw=b"",
            headers={},
            retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            synthetic_permit=_snapshot_permit(),
            max_bytes=4,
        )
    with pytest.raises(ContractError, match="empty, oversized"):
        store.land(
            source="fixture",
            url="https://example.invalid",
            http_status=200,
            raw=b"12345",
            headers={},
            retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            synthetic_permit=_snapshot_permit(),
            max_bytes=4,
        )


def test_nasdaq_identity_is_conservative_and_unknown_abstains(tmp_path) -> None:
    raw = (
        "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n"
        "Y|ABC|ABC COMMON STOCK|N|Q|N|100|N|N|ABC|ABC|N\n"
        "Y|SPY|SPDR S&P 500 ETF TRUST|P|Q|Y|100|N|N|SPY|SPY|N\n"
        "Y|WRT|SAMPLE WARRANT|N|Q|N|100|N|N|WRT|WRT|N\n"
        "Y|ODD|AMBIGUOUS SECURITY|N|Q|N|100|N|N|ODD|ODD|N\n"
        "Y|UNI|UNITED INDUSTRIES COMMON STOCK|N|Q|N|100|N|N|UNI|UNI|N\n"
        "Y|TEST|TEST COMMON STOCK|N|Q|N|100|Y|N|TEST|TEST|N\n"
        "N|BRK.A|BERKSHIRE HATHAWAY INC. COMMON STOCK|N| |N|1|N||BRK.A|BRK.A|N\n"
        "File Creation Time: 0715202601:30|||||||||||\n"
    ).encode()
    snapshot = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path).land(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=raw,
        headers={"etag": "fixture"},
        retrieved_at=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )
    records = parse_nasdaq_traded(snapshot, policy=_nasdaq_policy())
    with pytest.raises(ContractError, match="network as-received"):
        parse_nasdaq_traded(snapshot)
    network_registry = NetworkAcquisitionRegistry.load(
        Path(__file__).parents[1] / "config" / "network_acquisition_registry.json"
    )
    network_snapshot = AsReceivedSnapshotStore(
        tmp_path / "network-snapshots",
        allowed_root=tmp_path,
        acquisition_registry=network_registry,
    )._land_network_response(
        source="nasdaqtraded",
        requested_url=NASDAQ_TRADED_URL,
        response_url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=raw,
        headers={"content-type": "text/plain", "content-length": str(len(raw))},
        clock=TrustedClock.production(),
    )
    with pytest.raises(ContractError, match="byte count fails"):
        parse_nasdaq_traded(network_snapshot)
    with pytest.raises(ContractError, match="count drop"):
        parse_nasdaq_traded(
            snapshot,
            policy=replace(
                _nasdaq_policy(),
                maximum_drop_fraction=0.10,
                maximum_count_change_fraction=0.25,
            ),
            prior_accepted_record_count=10,
        )
    types = {record.symbol: record.security_type for record in records}
    assert types == {
        "ABC": SecurityType.STOCK,
        "SPY": SecurityType.ETF,
        "WRT": SecurityType.UNKNOWN,
        "ODD": SecurityType.UNKNOWN,
        "UNI": SecurityType.STOCK,
        "TEST": SecurityType.UNKNOWN,
        "BRK.A": SecurityType.STOCK,
    }
    assert not next(record for record in records if record.symbol == "ODD").eligible_type


def test_nasdaq_current_short_trailer_is_strictly_supported(tmp_path) -> None:
    raw = (
        "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n"
        "Y|ABC|ABC COMMON STOCK|N|Q|N|100|N|N|ABC|ABC|N\n"
        "File Creation Time: 0715202601:30|||||\n"
    ).encode()
    snapshot = AsReceivedSnapshotStore(
        tmp_path / "snapshots", allowed_root=tmp_path
    ).land(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=raw,
        headers={"content-type": "text/plain", "content-length": str(len(raw))},
        retrieved_at=datetime(2026, 7, 15, 6, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )
    records = parse_nasdaq_traded(snapshot, policy=_nasdaq_policy())
    assert len(records) == 1 and records[0].symbol == "ABC"
