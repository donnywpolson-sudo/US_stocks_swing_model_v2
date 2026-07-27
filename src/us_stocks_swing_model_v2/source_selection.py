from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

from .common import reject_link, require_contained_path, require_sha256
from .errors import ContractError
from .releases import ReleaseManifest, verify_accepted_release


def select_explicit_release(
    release_directory: Path,
    *,
    expected_dataset: str,
    expected_release_id: str,
    expected_project: str,
    accepted_release_root: Path,
    allowed_epoch_roles: Mapping[str, Iterable[str]],
    allowed_quality_states: Iterable[str],
) -> ReleaseManifest:
    """Select one exact release path; never search or fall back."""
    directory = Path(release_directory)
    root = Path(accepted_release_root)
    if not directory.is_absolute() or not root.is_absolute():
        raise ContractError("release directory and accepted root must be absolute")
    require_contained_path(directory, root)
    reject_link(directory)
    relative_parts = tuple(
        part.casefold()
        for part in directory.resolve(strict=True).relative_to(
            root.resolve(strict=True)
        ).parts
    )
    if any(
        part in {".staging", ".quarantine"} or part.startswith(".pending-")
        for part in relative_parts
    ):
        raise ContractError("pending/staging paths can never be active releases")
    expected_directory = root / expected_dataset / expected_release_id
    if directory.name != expected_release_id or directory.parent.resolve(strict=True) != expected_directory.parent.resolve(strict=True):
        raise ContractError("release path is not the exact accepted root/dataset/release ID")
    manifest = verify_accepted_release(directory, accepted_root=root)
    if manifest.project != expected_project:
        raise ContractError("explicit release project differs from requested project")
    if manifest.dataset != expected_dataset:
        raise ContractError("explicit release dataset differs from requested dataset")
    require_sha256(expected_release_id, "expected_release_id")
    if manifest.release_id != expected_release_id:
        raise ContractError("explicit release ID differs from the reviewed release")
    valid_roles = {
        "legacy_discovery_only",
        "qualification_evidence_only",
        "active_historical",
        "prospective_as_received",
        "derived_causal",
        "feature_only",
        "outcome_only",
    }
    epoch_roles: dict[str, set[str]] = {}
    for epoch, roles in allowed_epoch_roles.items():
        if type(epoch) is not str or not epoch:
            raise ContractError(
                "epoch-role allowlist keys must be exact nonempty text"
            )
        role_set = set(roles)
        if (
            not role_set
            or any(type(role) is not str for role in role_set)
            or not role_set <= valid_roles
        ):
            raise ContractError("epoch-role allowlist is absent or invalid")
        epoch_roles[epoch] = role_set
    if not epoch_roles:
        raise ContractError("epoch-role allowlist is absent or invalid")
    if manifest.source_epoch not in epoch_roles or manifest.role not in epoch_roles[manifest.source_epoch]:
        raise ContractError("release source epoch and role pairing is not permitted")
    qualities = set(allowed_quality_states)
    if not qualities or manifest.quality_state not in qualities:
        raise ContractError("release quality state is not permitted")
    return manifest
