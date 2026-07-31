from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

import us_stocks_swing_model_v2.cli.plan_legacy_discovery as planner_cli
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.legacy_discovery_bridge import (
    EXPECTED_EPOCHS,
    EXPECTED_KINDS,
    foundation_context_from_payload,
    load_foundation_plan_context,
    load_legacy_discovery_bridge_contract,
    plan_from_context,
)
from us_stocks_swing_model_v2.releases import ReleaseFile, ReleaseManifest


REPO = Path(__file__).resolve().parents[1]


def _release_binding(epoch: str, kind: str, *, row_count: int) -> dict[str, object]:
    bounds = {
        "hfdl_pitrading_consolidated": ("2010-01-04", "2022-03-03"),
        "hfdl_iex_only": ("2022-03-04", "2026-06-26"),
    }
    return {
        "dataset": f"{epoch}_{kind}",
        "epoch": epoch,
        "event_end": bounds[epoch][1],
        "event_start": bounds[epoch][0],
        "kind": kind,
        "manifest_sha256": sha256_bytes(f"{epoch}:{kind}:manifest".encode()),
        "phase": "bridge",
        "quality_state": "LEGACY_CAVEATED",
        "relative_directory": f"{epoch}_{kind}/{'a' * 64}",
        "release_id": sha256_bytes(f"{epoch}:{kind}:release".encode()),
        "role": "legacy_discovery_only",
        "row_count": row_count,
        "source_epoch": epoch,
    }


def _payload() -> dict[str, object]:
    epochs = {}
    for index, epoch in enumerate(EXPECTED_EPOCHS, start=1):
        epochs[epoch] = {
            kind: _release_binding(epoch, kind, row_count=index * 100)
            for kind in EXPECTED_KINDS
        }
    return {
        "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
        "point_in_time_safe": False,
        "epochs_may_be_pooled": False,
        "labels_emitted": False,
        "matured_outcomes_emitted": False,
        "model_or_evaluation_inputs_read": False,
        "wfa_executed": False,
        "real_history_hypothesis_executed": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
        "contract_id": "c" * 64,
        "contract": {
            "project": "US_stocks_swing_model_v2",
            "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
            "physical_hfdl_epochs": list(EXPECTED_EPOCHS),
            "historical_release_kinds": list(EXPECTED_KINDS),
            "epochs_may_be_pooled": False,
            "labels_allowed": False,
            "models_allowed": False,
            "wfa_allowed": False,
        },
        "historical_foundation": {
            "bridge_set": {},
            "build_id": "b" * 64,
            "epochs": epochs,
        },
        "calendar": {
            "release": {
                "dataset": "xnys_sessions",
                "role": "derived_causal",
                "quality_state": "PASS",
                "release_id": "d" * 64,
            }
        },
    }


def _manifest() -> ReleaseManifest:
    provisional = ReleaseManifest(
        schema_version=1,
        project="US_stocks_swing_model_v2",
        dataset="stock_historical_foundation_set",
        source_epoch="hfdl_two_epoch_legacy_discovery_no_pooling",
        role="legacy_discovery_only",
        quality_state="LEGACY_CAVEATED",
        created_at="2026-07-30T00:00:00Z",
        row_count=11,
        event_start="2000-01-03",
        event_end="2035-12-31",
        upstream_release_ids=(),
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
        files=(
            ReleaseFile(
                path="foundation_set.json",
                size=1,
                sha256="5" * 64,
            ),
        ),
        release_id="0" * 64,
    )
    return ReleaseManifest(
        **{
            **provisional.__dict__,
            "release_id": sha256_bytes(canonical_json_bytes(provisional.unsigned_dict())),
        }
    )


def _manifest_for_payload(raw: bytes) -> ReleaseManifest:
    baseline = _manifest()
    provisional = ReleaseManifest(
        **{
            **baseline.__dict__,
            "files": (
                ReleaseFile(
                    path="foundation_set.json",
                    size=len(raw),
                    sha256=sha256_bytes(raw),
                ),
            ),
            "release_id": "0" * 64,
        }
    )
    return ReleaseManifest(
        **{
            **provisional.__dict__,
            "release_id": sha256_bytes(canonical_json_bytes(provisional.unsigned_dict())),
        }
    )


def _foundation() -> dict[str, object]:
    return foundation_context_from_payload(
        _payload(),
        manifest=_manifest(),
        manifest_sha256="6" * 64,
        payload_sha256="7" * 64,
    )


def test_checked_in_contract_is_content_addressed_and_non_authorizing() -> None:
    contract = load_legacy_discovery_bridge_contract(REPO)
    unsigned = {name: value for name, value in contract.items() if name != "contract_id"}
    assert contract["contract_id"] == sha256_bytes(canonical_json_bytes(unsigned))
    assert contract["proxy_eligibility"]["trusted_sleeves"] == []
    assert contract["outcome_adapter"]["canonical_split_normalized_target_equivalent"] is False
    assert contract["outcome_adapter"]["compute_during_planning"] is False
    assert not any(contract["authorities"].values())


def test_synthetic_foundation_context_preserves_two_unpooled_epochs() -> None:
    foundation = _foundation()
    assert [value["epoch_id"] for value in foundation["epochs"]] == list(EXPECTED_EPOCHS)
    assert foundation["epochs_may_be_pooled"] is False
    assert foundation["point_in_time_safe"] is False
    assert foundation["direct_model_or_evaluation_inputs_allowed"] is False


def test_metadata_loader_needs_no_component_or_historical_row_files(
    tmp_path: Path,
) -> None:
    accepted = tmp_path / "accepted"
    payload_raw = canonical_json_bytes(_payload())
    manifest = _manifest_for_payload(payload_raw)
    release = accepted / "stock_historical_foundation_set" / manifest.release_id
    release.mkdir(parents=True)
    (release / "foundation_set.json").write_bytes(payload_raw)
    (release / "release_manifest.json").write_bytes(
        canonical_json_bytes(manifest.as_dict())
    )

    context = load_foundation_plan_context(release, accepted_root=accepted)

    assert context["foundation_release_id"] == manifest.release_id
    assert context["foundation_payload_sha256"] == sha256_bytes(payload_raw)
    assert [value["epoch_id"] for value in context["epochs"]] == list(EXPECTED_EPOCHS)
    assert sorted(path.name for path in release.iterdir()) == [
        "foundation_set.json",
        "release_manifest.json",
    ]


def test_plan_is_deterministic_separate_and_preregistration_blocked() -> None:
    contract = load_legacy_discovery_bridge_contract(REPO)
    foundation = _foundation()
    repository = {"head": "a" * 40, "tree": "b" * 40}
    first = plan_from_context(
        contract=contract,
        foundation=foundation,
        repository=repository,
    )
    second = plan_from_context(
        contract=contract,
        foundation=foundation,
        repository=repository,
    )
    assert first == second
    plan_id = first.pop("plan_id")
    assert plan_id == sha256_bytes(canonical_json_bytes(first))
    assert [value["epoch_id"] for value in first["epoch_plans"]] == list(EXPECTED_EPOCHS)
    assert first["cross_epoch_disposition"]["pooling_allowed"] is False
    assert first["cross_epoch_disposition"]["separate_wfa_plans_required"] is True
    assert first["metadata_validation_scope"] == {
        "release_manifest_validated": True,
        "foundation_set_payload_hash_validated": True,
        "component_row_payloads_opened": False,
        "complete_accepted_release_verification_performed": False,
        "complete_verification_required_before_derivation": True,
    }
    assert (
        first["future_derivative_release"][
            "full_foundation_release_verification_required_before_derivation"
        ]
        is True
    )
    assert first["preregistration_gate"]["registered_real_history_executor"] is None
    assert first["preregistration_gate"]["trial_counted"] is True
    assert first["output_disposition"]["historical_rows_read"] == 0
    assert first["output_disposition"]["outcomes_computed"] == 0
    assert first["output_disposition"]["models_fit"] == 0


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("epochs_may_be_pooled",), True, "safety boundary"),
        (("point_in_time_safe",), True, "safety boundary"),
        (
            ("direct_model_or_evaluation_inputs_allowed",),
            True,
            "safety boundary",
        ),
    ],
)
def test_plan_rejects_weakened_foundation_safety(
    path: tuple[str, ...],
    value: object,
    match: str,
) -> None:
    foundation = deepcopy(_foundation())
    foundation[path[0]] = value
    with pytest.raises(ContractError, match=match):
        plan_from_context(
            contract=load_legacy_discovery_bridge_contract(REPO),
            foundation=foundation,
            repository={"head": "a" * 40, "tree": "b" * 40},
        )


def test_foundation_context_rejects_epoch_or_component_drift() -> None:
    payload = _payload()
    payload["historical_foundation"]["epochs"].pop("hfdl_iex_only")
    with pytest.raises(ContractError, match="epoch census"):
        foundation_context_from_payload(
            payload,
            manifest=_manifest(),
            manifest_sha256="6" * 64,
            payload_sha256="7" * 64,
        )

    payload = _payload()
    payload["historical_foundation"]["epochs"]["hfdl_iex_only"].pop("outcome_inputs")
    with pytest.raises(ContractError, match="component census"):
        foundation_context_from_payload(
            payload,
            manifest=_manifest(),
            manifest_sha256="6" * 64,
            payload_sha256="7" * 64,
        )


def test_foundation_context_rejects_direct_model_or_wfa_permission() -> None:
    for field in ("models_allowed", "wfa_allowed", "labels_allowed"):
        payload = _payload()
        payload["contract"][field] = True
        with pytest.raises(ContractError, match="source contract"):
            foundation_context_from_payload(
                payload,
                manifest=_manifest(),
                manifest_sha256="6" * 64,
                payload_sha256="7" * 64,
            )


def test_cli_emits_only_supplied_plan_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = {
        "schema_version": 1,
        "mode": "LEGACY_DISCOVERY_PROXY_BRIDGE_PLAN_ONLY_NO_WRITES",
        "plan_id": "a" * 64,
    }
    monkeypatch.setattr(
        planner_cli,
        "build_legacy_discovery_bridge_plan",
        lambda *_args, **_kwargs: expected,
    )
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))
    assert (
        planner_cli.main(
            [
                "--foundation-set-directory",
                str(tmp_path / "synthetic-foundation"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == expected
    assert tuple(tmp_path.rglob("*")) == before


def test_planner_surface_has_no_row_reader_writer_or_execution_transport() -> None:
    module_source = (
        REPO / "src/us_stocks_swing_model_v2/legacy_discovery_bridge.py"
    ).read_text(encoding="utf-8")
    cli_source = (
        REPO / "src/us_stocks_swing_model_v2/cli/plan_legacy_discovery.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "pyarrow",
        "read_table",
        "read_parquet",
        "atomic_write",
        "open_without_redirects",
        '"--execute"',
        ".fit(",
    ):
        assert forbidden not in module_source
        assert forbidden not in cli_source


def test_contract_loader_rejects_tampered_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path
    (root / "config").mkdir()
    source = json.loads(
        (REPO / "config/legacy_discovery_bridge_contract.json").read_text(
            encoding="utf-8"
        )
    )
    source["authorities"]["training"] = True
    (root / "config/legacy_discovery_bridge_contract.json").write_text(
        json.dumps(source, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="grants authority"):
        load_legacy_discovery_bridge_contract(root)
