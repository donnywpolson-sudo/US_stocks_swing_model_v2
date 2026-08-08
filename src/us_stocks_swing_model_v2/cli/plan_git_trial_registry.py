"""Print the zero-write local Git/GitHub trial-registry readiness plan."""

from __future__ import annotations

import json
from pathlib import Path

from ..git_trial_registry import build_git_trial_registry_plan


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main() -> int:
    print(json.dumps(build_git_trial_registry_plan(_repo_root()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
