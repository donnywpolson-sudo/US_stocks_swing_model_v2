from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from urllib.request import Request

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import (
    ContractError,
    IntegrityError,
    NetworkGuardError,
)
from us_stocks_swing_model_v2.cli.qualify_free_sources import main as qualification_main
import us_stocks_swing_model_v2.cli.qualify_free_sources as qualification_cli
from us_stocks_swing_model_v2.providers.alpaca import (
    MAX_TRUSTED_REQUEST_AGE_MINUTES,
    AlpacaBarsPolicy,
    AlpacaBarsRequest,
    _valid_bar,
    guarded_fetch_json,
    qualify_landed_pages,
)
import us_stocks_swing_model_v2.providers.alpaca as alpaca_module
from us_stocks_swing_model_v2.providers.http import _RejectRedirects
from us_stocks_swing_model_v2.providers.nasdaq import (
    NASDAQ_TRADED_URL,
    NasdaqCompletenessPolicy,
    parse_nasdaq_traded,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    MAX_SNAPSHOT_BYTES,
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


def test_public_alpaca_annotations_resolve_runtime_types() -> None:
    hints = get_type_hints(qualify_landed_pages)
    assert hints["calendar_release_directory"] is Path
    assert hints["accepted_release_root"] is Path


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


def test_alpaca_request_time_is_bounded_by_trusted_execution_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_at = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    policy = AlpacaBarsPolicy(feed="iex")
    valid = AlpacaBarsRequest(
        ("ABC",),
        trusted_at - timedelta(days=1),
        trusted_at - timedelta(minutes=36),
        trusted_at - timedelta(minutes=MAX_TRUSTED_REQUEST_AGE_MINUTES),
    )
    valid.validate_against_trusted_time(policy, trusted_at)

    future = replace(
        valid,
        requested_at=trusted_at + timedelta(microseconds=1),
    )
    with pytest.raises(ContractError, match="later than trusted"):
        future.validate_against_trusted_time(policy, trusted_at)

    stale = replace(
        valid,
        requested_at=trusted_at
        - timedelta(
            minutes=MAX_TRUSTED_REQUEST_AGE_MINUTES,
            microseconds=1,
        ),
    )
    with pytest.raises(ContractError, match="stale"):
        stale.validate_against_trusted_time(policy, trusted_at)

    too_recent = replace(
        valid,
        requested_at=trusted_at,
        end=trusted_at - timedelta(minutes=19),
    )
    with pytest.raises(ContractError, match="trusted request time"):
        too_recent.validate_against_trusted_time(policy, trusted_at)

    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    monkeypatch.setattr(
        alpaca_module,
        "assert_authorized_network_request",
        lambda *args, **kwargs: pytest.fail(
            "authorization was reached before trusted-time rejection"
        ),
    )
    clock = TrustedClock.synthetic_fixed(
        trusted_at,
        permit=SyntheticOnlyPermit.create(
            fixture_id="alpaca-trusted-request-time",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )
    with pytest.raises(ContractError, match="later than trusted"):
        guarded_fetch_json(
            future,
            api_key_id="id",
            api_secret_key="secret",
            policy=policy,
            network_enabled=True,
            clock=clock,
            authorization_session=object(),
        )


def test_alpaca_bar_predicate_does_not_commit_timestamp_state() -> None:
    request = AlpacaBarsRequest(
        ("ABC",),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        datetime(2024, 1, 3, tzinfo=timezone.utc),
    )
    valid = {
        "t": "2024-01-01T05:00:00Z",
        "o": 10.0,
        "h": 11.0,
        "l": 9.0,
        "c": 10.5,
        "v": 100,
        "n": 10,
    }
    timestamps: list[datetime] = []
    assert _valid_bar(
        {**valid, "n": "invalid"},
        request,
        timestamps,
    ) is None
    assert timestamps == []
    assert _valid_bar(valid, request, timestamps) is not None
    assert timestamps == []
    non_midnight = {**valid, "t": "2024-01-01T06:00:00Z"}
    assert _valid_bar(non_midnight, request, timestamps) is not None
    assert timestamps == []
    assert _valid_bar(valid, request, timestamps) is not None


@pytest.mark.parametrize("invalid_volume", [1.5, 100.0, True, "100", -1])
def test_alpaca_bar_requires_exact_nonnegative_integer_volume(
    invalid_volume: object,
) -> None:
    request = AlpacaBarsRequest(
        ("ABC",),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        datetime(2024, 1, 3, tzinfo=timezone.utc),
    )
    valid = {
        "t": "2024-01-01T05:00:00Z",
        "o": 10.0,
        "h": 11.0,
        "l": 9.0,
        "c": 10.5,
        "v": 100,
    }
    assert _valid_bar(valid, request, []) is not None
    assert _valid_bar({**valid, "v": invalid_volume}, request, []) is None


@pytest.mark.parametrize(
    "invalid_trade_count",
    [1.5, 100.0, True, "100", -1, float("nan")],
)
def test_alpaca_bar_requires_exact_nonnegative_integer_trade_count(
    invalid_trade_count: object,
) -> None:
    request = AlpacaBarsRequest(
        ("ABC",),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        datetime(2024, 1, 3, tzinfo=timezone.utc),
    )
    valid = {
        "t": "2024-01-01T05:00:00Z",
        "o": 10.0,
        "h": 11.0,
        "l": 9.0,
        "c": 10.5,
        "v": 100,
        "n": 0,
        "vw": 10.25,
    }
    assert _valid_bar(valid, request, []) is not None
    assert _valid_bar({**valid, "n": 25}, request, []) is not None
    assert _valid_bar(
        {**valid, "n": invalid_trade_count},
        request,
        [],
    ) is None


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("FREE_SOURCE_QUALIFICATION_APPROVED", raising=False)

    def unexpected_network(*args, **kwargs):
        raise AssertionError("plan-only qualification attempted network access")

    monkeypatch.setattr(
        qualification_cli, "open_without_redirects", unexpected_network
    )
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))
    assert qualification_main([]) == 0
    assert '"mode": "plan_only"' in capsys.readouterr().out
    assert tuple(tmp_path.rglob("*")) == before
    with pytest.raises(NetworkGuardError, match="FREE_SOURCE_QUALIFICATION_APPROVED"):
        qualification_main(["--execute-network"])


def test_obsolete_authorization_arguments_are_rejected_without_writes(
    tmp_path: Path,
) -> None:
    for option in (
        "--emit-authorization-requests",
        "--authorization-request-directory",
        "--network-authorization",
        "--network-authority-registry",
        "--network-key-id",
        "--network-public-key-file",
    ):
        destination = tmp_path / option.removeprefix("--")
        with pytest.raises(SystemExit):
            qualification_main(["--nasdaq-only", option, str(destination)])
        assert not destination.exists()

    with pytest.raises(SystemExit):
        qualification_main(
            [
                "--plan-only",
                "--emit-authorization-requests",
                str(tmp_path / "forbidden"),
            ]
        )
    assert not (tmp_path / "forbidden").exists()


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
        local_integrity_verified = True

    class Store:
        def __init__(self, *args, **kwargs):
            pass

        def _land_network_response(self, **kwargs):
            return Snapshot()

    root = Path(__file__).resolve().parents[1]
    source_config = qualification_cli._load_source_config(root)
    source_config["snapshot_store_root"] = str(
        root / "data" / "vault" / "qualification" / "as_received"
    )
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    monkeypatch.setattr(
        qualification_cli,
        "open_without_redirects",
        lambda *args, **kwargs: Response(),
    )
    monkeypatch.setattr(qualification_cli, "AsReceivedSnapshotStore", Store)
    monkeypatch.setattr(
        qualification_cli,
        "_load_source_config",
        lambda _repo_root: source_config,
    )
    monkeypatch.setattr(
        qualification_cli,
        "start_local_network_execution",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        qualification_cli,
        "assert_local_network_request",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        qualification_cli,
        "_bind_authorized_network_response",
        lambda *args, **kwargs: object(),
    )
    assert qualification_main([
        "--execute-network",
        "--nasdaq-only",
    ]) == 0
    output = capsys.readouterr().out
    assert '"mode": "network_capture"' in output
    assert '"local_integrity_verified": true' in output
    assert '"attestation_request"' not in output
    assert '"record_count"' not in output
    assert '"nasdaq"' in output


def test_local_nasdaq_verification_is_offline_and_reports_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Store:
        def __init__(self, *args, **kwargs):
            pass

        def load(self, *args, **kwargs):
            return SimpleNamespace(
                source="nasdaqtraded",
                url=NASDAQ_TRADED_URL,
                snapshot_id="1" * 64,
                raw_sha256="3" * 64,
                retrieved_at=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
                local_integrity_verified=True,
            )

    class Record:
        file_created_at = datetime(2026, 7, 14, 22, tzinfo=timezone.utc)

    def unexpected_network(*args, **kwargs):
        raise AssertionError("offline attestation verification attempted network access")

    root = Path(__file__).resolve().parents[1]
    source_config = qualification_cli._load_source_config(root)
    source_config["snapshot_store_root"] = str(
        root / "data" / "vault" / "qualification" / "as_received"
    )
    monkeypatch.setattr(
        qualification_cli, "open_without_redirects", unexpected_network
    )
    monkeypatch.setattr(qualification_cli, "AsReceivedSnapshotStore", Store)
    monkeypatch.setattr(
        qualification_cli,
        "_load_source_config",
        lambda _repo_root: source_config,
    )
    observed: dict[str, object] = {}

    def parse_with_continuity(snapshot, **kwargs):
        observed.update(kwargs)
        return (Record(),)

    monkeypatch.setattr(
        qualification_cli,
        "parse_nasdaq_traded",
        parse_with_continuity,
    )
    assert qualification_main([
        "--verify-nasdaq-snapshot",
        str(tmp_path / "snapshot"),
        "--prior-nasdaq-accepted-record-count",
        "13050",
    ]) == 0
    output = capsys.readouterr().out
    assert '"mode": "verify_local_nasdaq_snapshot"' in output
    assert '"local_integrity_verified": true' in output
    assert '"record_count": 1' in output
    assert observed == {"prior_accepted_record_count": 13050}


def test_alpaca_response_is_bounded_and_retrieval_time_is_post_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        alpaca_module,
        "assert_authorized_network_request",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        alpaca_module,
        "_bind_authorized_network_response",
        lambda *args, **kwargs: object(),
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
        datetime.now(timezone.utc),
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


@pytest.mark.parametrize(
    "max_bytes",
    [True, False, 1.0, 1.5, "4", 0, -1, MAX_SNAPSHOT_BYTES + 1],
)
def test_snapshot_store_requires_exact_integer_byte_bound(
    tmp_path,
    max_bytes: object,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    with pytest.raises(ContractError, match="invalid byte bound"):
        store.land(
            source="fixture",
            url="https://example.invalid",
            http_status=200,
            raw=b"x",
            headers={},
            retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            synthetic_permit=_snapshot_permit(),
            max_bytes=max_bytes,
        )


def test_snapshot_store_accepts_exact_integer_byte_bound(tmp_path) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    snapshot = store.land(
        source="fixture",
        url="https://example.invalid",
        http_status=200,
        raw=b"x",
        headers={},
        retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
        max_bytes=1,
    )
    assert snapshot.raw_path.read_bytes() == b"x"


@pytest.mark.parametrize(
    ("http_status", "raw"),
    [
        (99, b"x"),
        (600, b"x"),
        (200, b""),
    ],
)
def test_snapshot_reload_rejects_hash_consistent_impossible_receipt_bounds(
    tmp_path: Path,
    http_status: int,
    raw: bytes,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    landed = store.land(
        source="fixture",
        url="https://example.invalid",
        http_status=200,
        raw=b"x",
        headers={},
        retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
        max_bytes=1,
    )
    receipt = json.loads(
        (landed.root / "receipt.json").read_text(encoding="utf-8")
    )
    receipt["http_status"] = http_status
    receipt["raw_bytes"] = len(raw)
    receipt["raw_sha256"] = sha256_bytes(raw)
    unsigned = {
        key: value
        for key, value in receipt.items()
        if key != "snapshot_id"
    }
    receipt["snapshot_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    forged = landed.root.parent / receipt["snapshot_id"]
    forged.mkdir()
    (forged / "raw.bin").write_bytes(raw)
    (forged / "headers.json").write_bytes(canonical_json_bytes(receipt["headers"]))
    (forged / "receipt.json").write_bytes(canonical_json_bytes(receipt))

    with pytest.raises(IntegrityError, match="outside bounds"):
        store.load(forged)


@pytest.mark.parametrize(
    "url",
    [
        123,
        "http://example.invalid/data",
        "https://user:secret@example.invalid/data",
        "https://example.invalid/data#fragment",
        " https://example.invalid/data",
        "https://example.invalid/data\n",
        "https://[",
        "https://example.invalid/\x7f",
    ],
)
def test_snapshot_store_requires_exact_credential_free_https_url(
    tmp_path,
    url: object,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    with pytest.raises(ContractError, match="snapshot URL"):
        store.land(
            source="fixture",
            url=url,
            http_status=200,
            raw=b"x",
            headers={},
            retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            synthetic_permit=_snapshot_permit(),
            max_bytes=1,
        )


def test_nasdaq_identity_is_conservative_and_unknown_abstains(tmp_path) -> None:
    raw = (
        "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n"
        "Y|ABC|ABC COMMON STOCK|N|Q|N|100|N|N|ABC|ABC|N\n"
        "Y|SPY|SPDR S&P 500 ETF TRUST|P|Q|Y|100|N|N|SPY|SPY|N\n"
        "Y|WRT|SAMPLE WARRANT|N|Q|N|100|N|N|WRT|WRT|N\n"
        "Y|EWRT|SAMPLE WARRANT ETF|N|Q|Y|100|N|N|EWRT|EWRT|N\n"
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
        "EWRT": SecurityType.UNKNOWN,
        "ODD": SecurityType.UNKNOWN,
        "UNI": SecurityType.STOCK,
        "TEST": SecurityType.UNKNOWN,
        "BRK.A": SecurityType.UNKNOWN,
    }
    assert not next(record for record in records if record.symbol == "ODD").eligible_type
    nontraded = next(record for record in records if record.symbol == "BRK.A")
    assert nontraded.nasdaq_traded is False
    assert nontraded.eligible_type is False


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


def test_production_nasdaq_parse_requires_continuity_evidence() -> None:
    snapshot = SimpleNamespace(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw_sha256="1" * 64,
        headers={"content-type": "text/plain"},
        trust_eligible=True,
    )
    with pytest.raises(ContractError, match="trusted prior accepted record count"):
        parse_nasdaq_traded(snapshot)
