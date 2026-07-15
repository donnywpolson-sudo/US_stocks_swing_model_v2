import json
import hashlib
from pathlib import Path


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
