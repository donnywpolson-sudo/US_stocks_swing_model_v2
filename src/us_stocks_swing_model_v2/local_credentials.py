from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping

from .errors import ContractError


CANONICAL_CREDENTIAL_VARIABLES = (
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "ALPHA_VANTAGE_API_KEY",
)
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def load_local_api_env(
    repository_root: Path,
    *,
    environment: MutableMapping[str, str] | None = None,
) -> dict[str, object]:
    """Load only canonical credentials from the repository-local ignored api.env.

    Existing process values win. Returned metadata contains names and presence
    states only; credential values are never returned, logged, or serialized.
    """

    root = Path(repository_root).resolve()
    path = root / "api.env"
    target = os.environ if environment is None else environment
    if path.is_symlink():
        raise ContractError("repository-local api.env is not a regular contained file")
    if not path.exists():
        return {
            "source": "api.env",
            "state": "NOT_FOUND",
            "loaded": [],
            "preserved": [],
            "presence": {name: bool(target.get(name)) for name in CANONICAL_CREDENTIAL_VARIABLES},
        }
    if not path.is_file() or path.resolve().parent != root:
        raise ContractError("repository-local api.env is not a regular contained file")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ContractError("repository-local api.env could not be loaded safely") from exc

    parsed: dict[str, str] = {}
    allowed = set(CANONICAL_CREDENTIAL_VARIABLES)
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ContractError(f"api.env entry {line_number} is malformed")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _NAME.fullmatch(name) or name not in allowed:
            raise ContractError(f"api.env entry {line_number} uses an unsupported variable name")
        if name in parsed:
            raise ContractError(f"api.env entry {line_number} duplicates a credential variable")
        parsed[name] = value

    loaded: list[str] = []
    preserved: list[str] = []
    for name in CANONICAL_CREDENTIAL_VARIABLES:
        if target.get(name):
            preserved.append(name)
        elif parsed.get(name):
            target[name] = parsed[name]
            loaded.append(name)
    return {
        "source": "api.env",
        "state": "LOADED",
        "loaded": loaded,
        "preserved": preserved,
        "presence": {name: bool(target.get(name)) for name in CANONICAL_CREDENTIAL_VARIABLES},
    }
