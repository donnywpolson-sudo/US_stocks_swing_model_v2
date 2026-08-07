from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.master_audit import (
    GROUP_FOOTER,
    FileBinding,
    _accepted_release_binding,
    _secret_like,
    _tracked_paths,
    build_dispatch,
    build_envelope,
    build_read_groups,
    load_policy,
)
from us_stocks_swing_model_v2.releases import (
    AtomicReleasePublisher,
    build_manifest,
)


REPO = Path(__file__).parents[1]


def test_checked_in_master_audit_policy_is_current_and_no_write() -> None:
    policy = load_policy(REPO)
    assert policy["mode"] == "MASTER_AUDIT_PLAN_ONLY_NO_WRITES"
    assert set(policy["targets"]) == {
        "REBUILD_COMPLETE",
        "HISTORICAL_RESEARCH_READY",
    }
    assert policy["output"] == {
        "destination": "CONVERSATION_ONLY",
        "retained_report": False,
        "generated_artifact_write": False,
    }
    assert policy["targets"]["HISTORICAL_RESEARCH_READY"][
        "requires_completed_target"
    ] == "REBUILD_COMPLETE"
    assert "august_raw_capture" in policy["prohibitions"]


def test_policy_uses_exact_release_ids_without_discovery_roots() -> None:
    policy = load_policy(REPO)
    for target in policy["targets"].values():
        releases = target["accepted_releases"]
        assert releases == sorted(set(releases))
        for relative in releases:
            parts = relative.split("/")
            assert parts[:3] == ["data", "vault", "accepted"]
            assert len(parts) == 5
            assert len(parts[-1]) == 64
    source = (REPO / "src/us_stocks_swing_model_v2/master_audit.py").read_text(
        encoding="utf-8"
    )
    assert ".rglob(" not in source
    assert "glob(" not in source


def test_read_groups_are_bounded_and_footer_terminated(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text("\n".join(f"line-{index}" for index in range(1, 14)) + "\n", encoding="utf-8")
    data = path.read_bytes()
    binding = FileBinding(
        path="review.md",
        bytes=len(data),
        sha256=sha256_bytes(data),
        line_count=13,
    )
    groups = build_read_groups(
        tmp_path,
        (binding,),
        maximum_lines=4,
        maximum_bytes=200,
    )
    assert [group.ordinal for group in groups] == list(range(1, len(groups) + 1))
    assert sum(group.numbered_line_count for group in groups) == 13
    assert all(group.numbered_line_count <= 4 for group in groups)
    assert all(group.rendered_utf8_bytes <= 200 for group in groups)
    assert all(group.as_dict()["required_footer"] == GROUP_FOOTER.rstrip("\n") for group in groups)


def test_unrenderable_single_line_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text("x" * 500, encoding="utf-8")
    data = path.read_bytes()
    binding = FileBinding("review.md", len(data), sha256_bytes(data), 1)
    with pytest.raises(ContractError, match="unrenderable audit line"):
        build_read_groups(tmp_path, (binding,), maximum_lines=10, maximum_bytes=100)


def test_envelope_requires_exact_runtime_and_clean_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.master_audit.platform.python_version",
        lambda: "3.11.8",
    )
    with pytest.raises(ContractError, match="exact Python 3.11.9"):
        build_envelope(tmp_path, "REBUILD_COMPLETE")

    monkeypatch.setattr(
        "us_stocks_swing_model_v2.master_audit.platform.python_version",
        lambda: "3.11.9",
    )
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.master_audit.load_policy", lambda root: {}
    )
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.master_audit._validate_root",
        lambda root, policy: tmp_path,
    )
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.master_audit._git", lambda *args: " M tracked.py"
    )
    with pytest.raises(ContractError, match="clean ordinary worktree"):
        build_envelope(tmp_path, "REBUILD_COMPLETE")


def test_secret_like_tracked_path_is_rejected_before_bytes_are_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = ["api.env"]
    policy = {
        "tracked_corpus": {
            "expected_path_count": 1,
            "expected_paths_sha256": sha256_bytes(canonical_json_bytes(paths)),
        }
    }
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.master_audit.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=b"api.env\x00", stderr=b""
        ),
    )
    assert _secret_like("api.env") is True
    with pytest.raises(ContractError, match="tracked secret-like path"):
        _tracked_paths(tmp_path, policy)


def test_exact_accepted_release_binding_verifies_manifest_and_payload(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "payload.json").write_bytes(b'{"ok":true}\n')
    manifest = build_manifest(
        stage,
        ("payload.json",),
        project="US_stocks_swing_model_v2",
        dataset="synthetic_audit",
        source_epoch="synthetic_audit_v1",
        role="derived_causal",
        quality_state="PASS",
        created_at="2026-08-07T00:00:00Z",
        row_count=1,
        event_start="2026-08-07",
        event_end="2026-08-07",
        upstream_release_ids=(),
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    accepted = tmp_path / "data/vault/accepted"
    published = AtomicReleasePublisher(accepted).publish(stage, manifest)
    relative = published.relative_to(tmp_path).as_posix()
    binding = _accepted_release_binding(tmp_path, relative)
    assert binding["release_id"] == manifest.release_id
    assert binding["dataset"] == "synthetic_audit"
    assert binding["payload_file_count"] == 1
    assert binding["payload_bytes"] == len(b'{"ok":true}\n')


def test_compact_dispatch_binds_commands_and_all_false_authorities() -> None:
    envelope_id = "a" * 64
    envelope = {
        "envelope_id": envelope_id,
        "target_state": "REBUILD_COMPLETE",
        "repository": {"root": "C:/repo", "head": "b" * 40},
        "specification": {"sha256": "c" * 64},
        "tracked_corpus": {
            "path_count": 2,
            "paths_sha256": "d" * 64,
            "files_sha256": "e" * 64,
        },
        "qualitative_review": {"path_count": 1, "group_count": 2},
        "accepted_releases": [{"release_id": "f" * 64}],
        "command_contract": {
            "commands": [
                {
                    "name": "Preflight",
                    "argv": ["python", "audit.py", "{ENVELOPE_ID}"],
                }
            ]
        },
        "reviewer_independence": {"fresh_reviewer": True},
        "output": {"destination": "CONVERSATION_ONLY"},
        "authorities": {"writes": False, "network_calls": False},
        "prohibitions": ["retry"],
    }
    first = build_dispatch(envelope)
    second = build_dispatch(envelope)
    assert first == second
    assert first["command_contract"]["commands"][0]["argv"][-1] == envelope_id
    assert set(first["authorities"].values()) == {False}
    unsigned = {key: value for key, value in first.items() if key != "dispatch_id"}
    assert first["dispatch_id"] == sha256_bytes(canonical_json_bytes(unsigned))


def test_master_audit_cli_source_has_direct_entrypoint() -> None:
    source = (REPO / "src/us_stocks_swing_model_v2/master_audit.py").read_text(
        encoding="utf-8"
    )
    assert 'if __name__ == "__main__":' in source
    assert "--approved-envelope-id" in source
    assert '"ReadGroup"' in source
