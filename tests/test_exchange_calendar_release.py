from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

import us_stocks_swing_model_v2.exchange_calendar as exchange_calendar_module
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.common import (
    canonical_json_bytes,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.exchange_calendar import (
    EXCHANGE_CALENDARS_VERSION,
    SYNTHETIC_CALENDAR_PUBLICATION_SCOPE,
    calendar_environment_hash,
    calendar_policy_hash,
    calendar_publication_binding_id,
    load_xnys_calendar_release,
    publish_xnys_calendar_release,
)
from us_stocks_swing_model_v2.releases import (
    AtomicReleasePublisher,
    build_manifest,
    verify_accepted_release,
)


def _publication_kwargs(
    root: Path,
    *,
    staging_root: Path,
    release_root: Path,
    start: date,
    end: date,
    created_at: str,
    code_hash: str,
    config_hash: str,
    environment_hash: str,
) -> dict[str, object]:
    binding_id = calendar_publication_binding_id(
        staging_root=staging_root,
        release_root=release_root,
        start=start,
        end=end,
        created_at=created_at,
        code_hash=code_hash,
        config_hash=config_hash,
        environment_hash=environment_hash,
    )
    return {
        "publication_synthetic_permit": SyntheticOnlyPermit.create(
            fixture_id=binding_id,
            scope=SYNTHETIC_CALENDAR_PUBLICATION_SCOPE,
        ),
        "publication_allowed_root": root,
    }


def test_xnys_release_is_content_addressed_and_pins_holiday_and_early_close(tmp_path) -> None:
    staging_root = tmp_path / "stage"
    release_root = tmp_path / "releases"
    start = date(2026, 7, 1)
    end = date(2026, 11, 30)
    created_at = "2026-07-15T00:00:00Z"
    code_hash = "1" * 64
    config_hash = calendar_policy_hash()
    environment_hash = calendar_environment_hash()
    release = publish_xnys_calendar_release(
        staging_root=staging_root,
        release_root=release_root,
        start=start,
        end=end,
        created_at=created_at,
        code_hash=code_hash,
        config_hash=config_hash,
        environment_hash=environment_hash,
        **_publication_kwargs(
            tmp_path,
            staging_root=staging_root,
            release_root=release_root,
            start=start,
            end=end,
            created_at=created_at,
            code_hash=code_hash,
            config_hash=config_hash,
            environment_hash=environment_hash,
        ),
    )
    loaded = load_xnys_calendar_release(
        release, accepted_release_root=tmp_path / "releases"
    )
    sessions = set(loaded.calendar.sessions)
    assert date(2026, 7, 3) not in sessions  # Independence Day observed.
    assert date(2026, 7, 6) in sessions
    rows = {row["session"]: row for row in loaded.schedule.to_pylist()}
    assert rows[date(2026, 11, 27)]["early_close"] is True  # Day after Thanksgiving.
    assert loaded.provenance["calendar_version"] == EXCHANGE_CALENDARS_VERSION
    assert loaded.calendar.release_id == release.name
    assert loaded.calendar.trust_eligible
    assert len(loaded.calendar.verification_receipt_id) == 64
    assert "never_infer_sessions_from_observed_bars" in loaded.provenance["source_contract"]


def test_xnys_loader_rejects_installed_calendar_version_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = {
        "staging_root": tmp_path / "stage",
        "release_root": tmp_path / "releases",
        "start": date(2026, 7, 1),
        "end": date(2026, 7, 10),
        "created_at": "2026-07-15T00:00:00Z",
        "code_hash": "1" * 64,
        "config_hash": calendar_policy_hash(),
        "environment_hash": calendar_environment_hash(),
    }
    release = publish_xnys_calendar_release(
        **kwargs,
        **_publication_kwargs(tmp_path, **kwargs),
    )
    monkeypatch.setattr(
        exchange_calendar_module.importlib.metadata,
        "version",
        lambda distribution: "0.0.0",
    )

    with pytest.raises(ContractError, match="runtime differs"):
        load_xnys_calendar_release(
            release,
            accepted_release_root=tmp_path / "releases",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_fingerprint", "f" * 64),
        ("config_hash", "e" * 64),
        ("environment_hash", "d" * 64),
    ],
)
def test_xnys_loader_rejects_semantically_unbound_manifest_closure(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    kwargs = {
        "staging_root": tmp_path / "base-stage",
        "release_root": tmp_path / "base-releases",
        "start": date(2026, 7, 1),
        "end": date(2026, 7, 10),
        "created_at": "2026-07-15T00:00:00Z",
        "code_hash": "1" * 64,
        "config_hash": calendar_policy_hash(),
        "environment_hash": calendar_environment_hash(),
    }
    base = publish_xnys_calendar_release(
        **kwargs,
        **_publication_kwargs(tmp_path, **kwargs),
    )
    original = verify_accepted_release(
        base,
        accepted_root=tmp_path / "base-releases",
    )
    stage = tmp_path / f"poison-{field}"
    stage.mkdir()
    for name in ("sessions.parquet", "provenance.json"):
        (stage / name).write_bytes((base / name).read_bytes())
    manifest_fields = {
        "schema_fingerprint": original.schema_fingerprint,
        "code_hash": original.code_hash,
        "config_hash": original.config_hash,
        "environment_hash": original.environment_hash,
    }
    manifest_fields[field] = value
    manifest = build_manifest(
        stage,
        ["sessions.parquet", "provenance.json"],
        project=original.project,
        dataset=original.dataset,
        source_epoch=original.source_epoch,
        role=original.role,
        quality_state=original.quality_state,
        created_at=original.created_at,
        row_count=original.row_count,
        event_start=original.event_start,
        event_end=original.event_end,
        **manifest_fields,
    )
    poison_root = tmp_path / f"poison-releases-{field}"
    poisoned = AtomicReleasePublisher(poison_root).publish(stage, manifest)

    with pytest.raises(IntegrityError, match="manifest closure"):
        load_xnys_calendar_release(
            poisoned,
            accepted_release_root=poison_root,
        )


def test_xnys_release_requires_exact_action_permit_before_mutation(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "stage"
    release_root = tmp_path / "releases"
    kwargs = {
        "staging_root": staging_root,
        "release_root": release_root,
        "start": date(2026, 7, 1),
        "end": date(2026, 7, 2),
        "created_at": "2026-07-15T00:00:00Z",
        "code_hash": "1" * 64,
        "config_hash": calendar_policy_hash(),
        "environment_hash": calendar_environment_hash(),
    }
    with pytest.raises(ContractError, match="synthetic-only permit"):
        publish_xnys_calendar_release(**kwargs)
    assert not staging_root.exists()
    assert not release_root.exists()

    wrong = SyntheticOnlyPermit.create(
        fixture_id="wrong-calendar-request",
        scope=SYNTHETIC_CALENDAR_PUBLICATION_SCOPE,
    )
    with pytest.raises(ContractError, match="exact request"):
        publish_xnys_calendar_release(
            **kwargs,
            publication_synthetic_permit=wrong,
            publication_allowed_root=tmp_path,
        )
    assert not staging_root.exists()
    assert not release_root.exists()


def test_xnys_publication_rejects_unpinned_closure_before_mutation(
    tmp_path: Path,
) -> None:
    kwargs = {
        "staging_root": tmp_path / "stage",
        "release_root": tmp_path / "releases",
        "start": date(2026, 7, 1),
        "end": date(2026, 7, 2),
        "created_at": "2026-07-15T00:00:00Z",
        "code_hash": "1" * 64,
        "config_hash": "2" * 64,
        "environment_hash": calendar_environment_hash(),
    }
    with pytest.raises(ContractError, match="pinned policy/environment"):
        publish_xnys_calendar_release(
            **kwargs,
            **_publication_kwargs(tmp_path, **kwargs),
        )
    assert not kwargs["staging_root"].exists()
    assert not kwargs["release_root"].exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("calendar_package", "other-package"),
        ("first_session", "2026-07-02"),
        ("last_session", "2026-07-09"),
        ("early_close_count", 99),
        ("created_at", "2026-07-15T00:01:00Z"),
        ("requested_start", "2026-06-30"),
        ("requested_end", "2026-07-13"),
    ),
)
def test_xnys_loader_reconciles_every_provenance_claim(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    staging_root = tmp_path / "base-stage"
    release_root = tmp_path / "base-releases"
    kwargs = {
        "staging_root": staging_root,
        "release_root": release_root,
        "start": date(2026, 7, 1),
        "end": date(2026, 7, 10),
        "created_at": "2026-07-15T00:00:00Z",
        "code_hash": "1" * 64,
        "config_hash": calendar_policy_hash(),
        "environment_hash": calendar_environment_hash(),
    }
    base = publish_xnys_calendar_release(
        **kwargs,
        **_publication_kwargs(tmp_path, **kwargs),
    )
    original = verify_accepted_release(
        base,
        accepted_root=release_root,
    )
    stage = tmp_path / "poison-stage"
    stage.mkdir()
    (stage / "sessions.parquet").write_bytes(
        (base / "sessions.parquet").read_bytes()
    )
    provenance = json.loads(
        (base / "provenance.json").read_text(encoding="utf-8")
    )
    provenance[field] = value
    (stage / "provenance.json").write_bytes(
        canonical_json_bytes(provenance)
    )
    manifest = build_manifest(
        stage,
        ["sessions.parquet", "provenance.json"],
        project=original.project,
        dataset=original.dataset,
        source_epoch=original.source_epoch,
        role=original.role,
        quality_state=original.quality_state,
        created_at=original.created_at,
        row_count=original.row_count,
        event_start=original.event_start,
        event_end=original.event_end,
        schema_fingerprint=original.schema_fingerprint,
        code_hash=original.code_hash,
        config_hash=original.config_hash,
        environment_hash=original.environment_hash,
    )
    poison_root = tmp_path / "poison-releases"
    poisoned = AtomicReleasePublisher(poison_root).publish(stage, manifest)
    with pytest.raises(IntegrityError, match="XNYS calendar"):
        load_xnys_calendar_release(
            poisoned,
            accepted_release_root=poison_root,
        )


def test_checked_in_calendar_receipt_binds_policy_code_and_non_authority() -> None:
    root = Path(__file__).resolve().parents[1]
    payload = json.loads(
        (root / "config" / "xnys_calendar_release_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    receipt_id = payload.pop("receipt_id")
    assert sha256_bytes(canonical_json_bytes(payload)) == receipt_id
    assert payload["policy_sha256"] == sha256_file(
        root / "config" / "xnys_calendar_policy.json"
    )
    # This immutable receipt authenticates the historical generator revision;
    # it is not a moving certification of the current source tree.
    require_sha256(payload["code_sha256"], "historical calendar code_sha256")
    assert payload["environment_sha256"] == sha256_file(
        root / "config" / "environment.lock.json"
    )
    assert payload["execution_authority"] is False
    assert payload["session_count"] == 9049
