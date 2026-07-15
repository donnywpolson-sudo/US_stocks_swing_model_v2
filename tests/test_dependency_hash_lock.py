from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename

from us_stocks_swing_model_v2.common import sha256_file


REPO = Path(__file__).parents[1]
PIN = re.compile(r"^([^=\s]+)==([^\s]+) --hash=sha256:([0-9a-f]{64})$")


def _plain_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==", 1)
        pins[canonicalize_name(name)] = version
    return pins


def test_windows_cp311_binary_closure_is_exact_and_hash_locked() -> None:
    lines = (REPO / "requirements.sha256.lock").read_text(encoding="utf-8").splitlines()
    wheel: str | None = None
    locked: dict[str, tuple[str, str, str]] = {}
    for raw in lines:
        line = raw.strip()
        if line.startswith("# target-wheel: "):
            wheel = line.removeprefix("# target-wheel: ")
            continue
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        assert match is not None
        assert wheel is not None
        name, version, digest = match.groups()
        parsed_name, parsed_version, build, tags = parse_wheel_filename(wheel)
        assert not build
        assert canonicalize_name(name) == canonicalize_name(parsed_name)
        assert version == str(parsed_version)
        assert all(
            (tag.interpreter in {"py2", "py3"} and tag.abi == "none" and tag.platform == "any")
            or (tag.interpreter == "cp311" and tag.abi == "cp311" and tag.platform == "win_amd64")
            for tag in tags
        )
        normalized = canonicalize_name(name)
        assert normalized not in locked
        locked[normalized] = (version, wheel, digest)
        wheel = None
    assert wheel is None
    plain = _plain_pins(REPO / "requirements.lock")
    assert {name: version for name, (version, _, _) in locked.items()} == plain
    assert len(locked) == 22

    environment = json.loads((REPO / "config" / "environment.lock.json").read_text(encoding="utf-8"))
    assert environment["target"] == {
        "implementation": "cpython",
        "python_tag": "cp311",
        "abi": "cp311",
        "platform_tag": "win_amd64",
        "machine": "AMD64",
    }
    assert environment["binary_closure_package_count"] == len(locked)
    assert environment["requirements_lock_sha256"] == sha256_file(REPO / "requirements.lock")
    assert environment["requirements_sha256_lock_sha256"] == sha256_file(
        REPO / "requirements.sha256.lock"
    )

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    direct = project["project"]["dependencies"] + project["project"]["optional-dependencies"]["test"]
    direct.append(project["build-system"]["requires"][0])
    for requirement in direct:
        name, version = requirement.split("==", 1)
        assert locked[canonicalize_name(name)][0] == version
