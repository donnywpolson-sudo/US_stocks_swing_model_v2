from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import sys
from pathlib import Path

from .common import canonical_json_bytes, sha256_bytes, sha256_file
from .errors import ContractError


PACKAGE_DISTRIBUTIONS = {
    "boto3": "boto3",
    "exchange-calendars": "exchange-calendars",
    "joblib": "joblib",
    "numpy": "numpy",
    "pandas": "pandas",
    "pyarrow": "pyarrow",
    "PyYAML": "PyYAML",
    "pytest": "pytest",
    "scikit-learn": "scikit-learn",
    "scipy": "scipy",
    "setuptools": "setuptools",
}
_PLAIN_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")
_HASHED_PIN = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s]+) --hash=sha256:[0-9a-f]{64}$"
)


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _locked_distribution_versions(
    plain_path: Path,
    hashed_path: Path,
) -> dict[str, str]:
    def parse(path: Path, pattern: re.Pattern[str]) -> dict[str, str]:
        pins: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ContractError("dependency lock is unreadable") from exc
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = pattern.fullmatch(line)
            if match is None:
                raise ContractError("dependency lock contains a malformed pin")
            name, version = match.groups()
            canonical_name = _canonical_distribution_name(name)
            if canonical_name in pins:
                raise ContractError(
                    "dependency lock contains a duplicate distribution"
                )
            pins[canonical_name] = version
        if not pins:
            raise ContractError("dependency lock contains no distributions")
        return dict(sorted(pins.items()))

    plain = parse(plain_path, _PLAIN_PIN)
    hashed = parse(hashed_path, _HASHED_PIN)
    if plain != hashed:
        raise ContractError("plain and hash-locked dependency closures differ")
    return hashed


def validate_environment_lock(path: Path) -> str:
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("environment lock is missing or invalid") from exc
    if (
        not isinstance(lock, dict)
        or type(lock.get("schema_version")) is not int
        or lock.get("schema_version") != 1
        or lock.get("python") != platform.python_version()
    ):
        raise ContractError("Python runtime differs from environment lock")
    if sys.version_info[:3] != (3, 11, 9):
        raise ContractError("research runtime is pinned to Python 3.11.9")
    expected_target = {
        "implementation": "cpython",
        "python_tag": "cp311",
        "abi": "cp311",
        "platform_tag": "win_amd64",
        "machine": "AMD64",
    }
    if (
        lock.get("platform") != platform.system()
        or lock.get("target") != expected_target
        or sys.implementation.name != expected_target["implementation"]
        or platform.machine() != expected_target["machine"]
    ):
        raise ContractError("runtime platform differs from the Windows CPython 3.11 lock")
    packages = lock.get("packages", {})
    if not isinstance(packages, dict) or set(packages) != set(PACKAGE_DISTRIBUTIONS):
        raise ContractError("environment package registry differs from the exact runtime contract")
    root = Path(path).resolve(strict=True).parent.parent
    if (
        sha256_file(root / "requirements.lock") != lock.get("requirements_lock_sha256")
        or sha256_file(root / "requirements.sha256.lock")
        != lock.get("requirements_sha256_lock_sha256")
    ):
        raise ContractError("dependency lock bytes differ from the environment contract")
    closure = _locked_distribution_versions(
        root / "requirements.lock",
        root / "requirements.sha256.lock",
    )
    if (
        type(lock.get("binary_closure_package_count")) is not int
        or len(closure) != lock["binary_closure_package_count"]
    ):
        raise ContractError("binary dependency closure count differs from the environment contract")
    for logical_name, expected in packages.items():
        distribution = PACKAGE_DISTRIBUTIONS.get(logical_name)
        if (
            distribution is None
            or closure.get(_canonical_distribution_name(distribution)) != expected
        ):
            raise ContractError(
                f"direct package registry differs from closure: {logical_name}"
            )
    for distribution, expected in closure.items():
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ContractError(
                f"locked distribution is not installed: {distribution}"
            ) from exc
        if installed != expected:
            raise ContractError(
                f"package runtime differs from lock: {distribution}"
            )
    return sha256_bytes(canonical_json_bytes(lock))
