from __future__ import annotations

import importlib.metadata
import json
import platform
import sys
from pathlib import Path

from .common import canonical_json_bytes, sha256_bytes, sha256_file
from .errors import ContractError


PACKAGE_DISTRIBUTIONS = {
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


def validate_environment_lock(path: Path) -> str:
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("environment lock is missing or invalid") from exc
    if lock.get("schema_version") != 1 or lock.get("python") != platform.python_version():
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
    for logical_name, expected in packages.items():
        distribution = PACKAGE_DISTRIBUTIONS.get(logical_name)
        if distribution is None or importlib.metadata.version(distribution) != expected:
            raise ContractError(f"package runtime differs from lock: {logical_name}")
    root = Path(path).resolve(strict=True).parent.parent
    if (
        sha256_file(root / "requirements.lock") != lock.get("requirements_lock_sha256")
        or sha256_file(root / "requirements.sha256.lock")
        != lock.get("requirements_sha256_lock_sha256")
    ):
        raise ContractError("dependency lock bytes differ from the environment contract")
    pinned_lines = [
        line
        for line in (root / "requirements.sha256.lock").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    if len(pinned_lines) != lock.get("binary_closure_package_count"):
        raise ContractError("binary dependency closure count differs from the environment contract")
    return sha256_bytes(canonical_json_bytes(lock))
