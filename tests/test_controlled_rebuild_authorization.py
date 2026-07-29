import json
import hashlib
from pathlib import Path
import re


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_controlled_rebuild_authorization_is_exact_and_non_alpha() -> None:
    path = Path(__file__).resolve().parents[1] / "config" / "controlled_rebuild_authorization.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    authorization_id = payload.pop("authorization_id")
    assert sha256_json(payload) == authorization_id == (
        "dd131238845c26cd9dca58aa6b0986e5ec46238881cecc411af2ddbb58b5bbf7"
    )
    assert payload["data_reuse_policy"] == {
        "blanket_redownload_allowed": False,
        "copy_mode": "HASH_VERIFIED_COPY_NOT_MOVE",
        "links_allowed": False,
        "legacy_bytes_remain_unchanged": True,
    }
    assert "real_history_hypothesis_or_wfa_execution" in payload["hard_pauses"]
    assert "candidate_sealing" in payload["hard_pauses"]
    assert set(payload["allowed_actions"]) >= {
        "hash_copy_approved_legacy_data",
        "bounded_free_alpaca_qualification",
        "bounded_free_nasdaq_qualification",
    }


def test_foundation_refresh_authorization_is_exact_one_shot_and_non_alpha() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "foundation_refresh_authorization.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    authorization_id = payload.pop("authorization_id")
    assert sha256_json(payload) == authorization_id == (
        "4d48145afee8a1713ad3f7321d4456449615eed7a14094da02cead38af07a062"
    )
    assert payload["schema_version"] == 2
    assert payload["authorization_version"] == "2.0.0"
    assert payload["maximum_commits_after_base"] == 2
    assert payload["required_substantive_commits_after_base"] == 1
    assert payload["required_coordination_commits_after_base"] == 1
    assert payload["substantive_commit_paths"] == [
        "config/foundation_refresh_authorization.json",
        "src/us_stocks_swing_model_v2/foundation_orchestrator.py",
        "tests/test_controlled_rebuild_authorization.py",
        "tests/test_foundation_orchestrator.py",
    ]
    assert payload["coordination_commit_paths"] == ["CODEX_HANDOFF.md"]
    assert payload["authorization_class"] == (
        "ONE_SHOT_NON_ACTIVE_FOUNDATION_SUCCESSOR_REFRESH"
    )
    assert re.fullmatch(r"[0-9a-f]{40}", payload["authorized_base_commit"])
    assert payload["maximum_distinct_builds"] == 1
    assert payload["work_root"] == "data/w/r"
    work = path.parents[1] / payload["work_root"]
    worst_case = (
        work
        / "b"
        / "hfdl_foundation"
        / ("f" * 32)
        / "stages"
        / "hfdl_pitrading_consolidated_feature_inputs"
        / "data"
        / ("." + "f" * 64 + ".parquet." + "f" * 8 + ".tmp")
    )
    assert len(str(worst_case)) < 250
    assert payload["idempotent_resume_same_build_allowed"] is True
    assert payload["prior_releases_immutable"] is True
    assert payload["provider_calls_allowed"] is False
    assert payload["model_or_wfa_allowed"] is False
    assert payload["legacy_paths_allowed"] is False
