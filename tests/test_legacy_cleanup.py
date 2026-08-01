from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.legacy_cleanup import build_cleanup_plan, write_cleanup_plan


def _policy(root: Path, targets: list[str], protected: list[str] | None = None) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "legacy_cleanup_policy.json").write_text(json.dumps({"mode": "PLAN_ONLY_NO_DELETION", "target_roots": targets, "protected_prefixes": protected or ["retain"]}), encoding="utf-8")


def test_cleanup_inventory_is_hash_bound_and_plan_only(tmp_path: Path) -> None:
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "item.bin").write_bytes(b"old")
    _policy(tmp_path, ["old"])
    plan = build_cleanup_plan(tmp_path)
    assert plan["file_count"] == 1
    assert plan["total_bytes"] == 3
    assert plan["execution_authorized"] is False
    assert (tmp_path / "old" / "item.bin").read_bytes() == b"old"


def test_cleanup_inventory_rejects_links_and_protected_paths(tmp_path: Path) -> None:
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "item.bin").write_bytes(b"old")
    _policy(tmp_path, ["old"], ["old/retain"])
    (tmp_path / "old" / "item.bin").rename(tmp_path / "old" / "retain")
    with pytest.raises(ContractError, match="protected"):
        build_cleanup_plan(tmp_path)


def test_cleanup_writer_reloads_and_rejects_census_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "item.bin").write_bytes(b"old")
    _policy(tmp_path, ["old"])
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.legacy_cleanup.repository_binding",
        lambda root, expected_commit: {"commit": expected_commit, "tree": "a" * 40},
    )
    result = write_cleanup_plan(tmp_path, expected_commit="b" * 40, created_at="2026-08-01T00:00:00Z")
    assert json.loads((Path(result["directory"]) / "cleanup_plan.json").read_text()) == result["plan"]
    assert result["receipt"]["execution_authorized"] is False
    with pytest.raises(ContractError, match="already exists"):
        write_cleanup_plan(tmp_path, expected_commit="b" * 40, created_at="2026-08-01T00:00:00Z")
