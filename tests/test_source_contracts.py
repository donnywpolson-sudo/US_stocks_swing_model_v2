from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

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
from us_stocks_swing_model_v2.providers.http import _RejectRedirects
from us_stocks_swing_model_v2.providers.network_authorization import (
    NetworkRequestPlan,
)
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

    monkeypatch.setattr(
        qualification_cli, "open_without_redirects", unexpected_network
    )
    assert qualification_main([]) == 0
    assert '"mode": "plan_only"' in capsys.readouterr().out
    with pytest.raises(NetworkGuardError, match="FREE_SOURCE_QUALIFICATION_APPROVED"):
        qualification_main(["--execute-network"])


def test_nasdaq_only_capture_does_not_require_calendar_or_claim_qualification(
    tmp_path: Path,
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
        snapshot_id = "1" * 64
        root = Path("C:/fixture/snapshot")
        raw_sha256 = "2" * 64
        retrieved_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
        trust_eligible = False

    class Store:
        def __init__(self, *args, **kwargs):
            pass

        def _land_network_response(self, **kwargs):
            return Snapshot()

    class UseStore:
        def __init__(self, *args, **kwargs):
            pass

        def authorize(self, **kwargs):
            return object()

    root = Path(__file__).resolve().parents[1]
    registry_contract = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json"
    )
    request_plan = NetworkRequestPlan.create(
        registry=registry_contract,
        source="nasdaqtraded",
        initial_url=NASDAQ_TRADED_URL,
        timeout_seconds=30,
        max_response_bytes=qualification_cli.MAX_NASDAQ_RESPONSE_BYTES,
        max_pages=1,
        pagination_parameter=None,
    )
    registry = tmp_path / "authority.json"
    public_key = tmp_path / "public.jwk"
    authorization = tmp_path / "authorization.json"
    public_key.write_bytes(b"public")
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    monkeypatch.setattr(
        qualification_cli,
        "open_without_redirects",
        lambda *args, **kwargs: Response(),
    )
    monkeypatch.setattr(qualification_cli, "AsReceivedSnapshotStore", Store)
    monkeypatch.setattr(qualification_cli, "NetworkAuthorizationUseStore", UseStore)
    monkeypatch.setattr(
        qualification_cli,
        "assert_authorized_network_request",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        qualification_cli, "load_external_authority", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        qualification_cli,
        "load_signed_authorization_receipt",
        lambda path: SimpleNamespace(subject_id=request_plan.plan_id),
    )
    monkeypatch.setattr(
        qualification_cli,
        "network_acquisition_attestation_bindings",
        lambda snapshot: {"raw_sha256": snapshot.raw_sha256},
    )
    assert qualification_main([
        "--execute-network",
        "--nasdaq-only",
        "--network-authorization",
        str(authorization),
        "--network-authority-registry",
        str(registry),
        "--network-key-id",
        "external-user",
        "--network-public-key-file",
        str(public_key),
    ]) == 0
    output = capsys.readouterr().out
    assert '"mode": "network_capture"' in output
    assert '"trust_eligible": false' in output
    assert '"attestation_request"' in output
    assert '"record_count"' not in output
    assert '"nasdaq"' in output


def test_attested_nasdaq_verification_is_offline_and_reports_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Store:
        def __init__(self, *args, **kwargs):
            pass

        def load_attested(self, *args, **kwargs):
            return SimpleNamespace(
                source="nasdaqtraded",
                url=NASDAQ_TRADED_URL,
                snapshot_id="1" * 64,
                acquisition_attestation=SimpleNamespace(receipt_id="2" * 64),
                raw_sha256="3" * 64,
                retrieved_at=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
                trust_eligible=True,
            )

    class Record:
        file_created_at = datetime(2026, 7, 14, 22, tzinfo=timezone.utc)

    def unexpected_network(*args, **kwargs):
        raise AssertionError("offline attestation verification attempted network access")

    attestation = tmp_path / "attestation.json"
    registry = tmp_path / "authority-registry.json"
    public_key = tmp_path / "public.jwk"
    for path in (attestation, registry, public_key):
        path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        qualification_cli, "open_without_redirects", unexpected_network
    )
    monkeypatch.setattr(qualification_cli, "AsReceivedSnapshotStore", Store)
    monkeypatch.setattr(
        qualification_cli,
        "load_external_authority",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        qualification_cli,
        "parse_nasdaq_traded",
        lambda snapshot: (Record(),),
    )
    assert qualification_main([
        "--verify-nasdaq-snapshot",
        str(tmp_path / "snapshot"),
        "--acquisition-attestation",
        str(attestation),
        "--attestation-authority-registry",
        str(registry),
        "--attestation-key-id",
        "external-user",
        "--attestation-public-key-file",
        str(public_key),
    ]) == 0
    output = capsys.readouterr().out
    assert '"mode": "verify_attested_nasdaq_snapshot"' in output
    assert '"trust_eligible": true' in output
    assert '"record_count": 1' in output


def test_alpaca_response_is_bounded_and_retrieval_time_is_post_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        alpaca_module,
        "assert_authorized_network_request",
        lambda *args, **kwargs: None,
    )

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

    monkeypatch.setattr(alpaca_module, "open_without_redirects", open_response)
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
        authorization_session=object(),
    )
    assert evidence.retrieved_at >= observed["read_finished"]
    assert "feed=iex" in evidence.url
    assert "asof=" not in evidence.url

    class Redirected(Response):
        def geturl(self) -> str:
            return "https://example.invalid/redirected"

    monkeypatch.setattr(
        alpaca_module, "open_without_redirects", lambda *args, **kwargs: Redirected()
    )
    with pytest.raises(ContractError, match="redirected"):
        guarded_fetch_json(
            request,
            api_key_id="id",
            api_secret_key="secret",
            policy=AlpacaBarsPolicy(feed="iex"),
            network_enabled=True,
            authorization_session=object(),
        )

    monkeypatch.setattr(alpaca_module, "MAX_ALPACA_RESPONSE_BYTES", 4)

    class Oversized(Response):
        def read(self, limit: int) -> bytes:
            return b"12345"

    def open_oversized(http_request, *args, **kwargs):
        observed["request_url"] = http_request.full_url
        return Oversized()

    monkeypatch.setattr(alpaca_module, "open_without_redirects", open_oversized)
    with pytest.raises(ContractError, match="bounded byte"):
        guarded_fetch_json(
            request,
            api_key_id="id",
            api_secret_key="secret",
            policy=AlpacaBarsPolicy(feed="sip"),
            network_enabled=True,
            authorization_session=object(),
        )


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_credentialed_redirect_is_rejected_before_followup_request(status: int) -> None:
    request = Request(
        "https://data.alpaca.markets/v2/stocks/bars",
        headers={
            "APCA-API-KEY-ID": "id",
            "APCA-API-SECRET-KEY": "secret",
        },
    )
    with pytest.raises(NetworkGuardError, match="before retransmission"):
        _RejectRedirects().redirect_request(
            request,
            object(),
            status,
            "redirect",
            {},
            "https://example.invalid/credential-target",
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
    assert network_snapshot.acquisition_mode == "NETWORK_AS_RECEIVED"
    assert network_snapshot.trust_eligible is False
    with pytest.raises(ContractError, match="network as-received"):
        parse_nasdaq_traded(network_snapshot)
    with pytest.raises(ContractError, match="status is not approved"):
        AsReceivedSnapshotStore(
            tmp_path / "network-error-snapshots",
            allowed_root=tmp_path,
            acquisition_registry=network_registry,
        )._land_network_response(
            source="nasdaqtraded",
            requested_url=NASDAQ_TRADED_URL,
            response_url=NASDAQ_TRADED_URL,
            http_status=503,
            raw=b"service unavailable",
            headers={"content-type": "text/plain"},
            clock=TrustedClock.production(),
        )
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
