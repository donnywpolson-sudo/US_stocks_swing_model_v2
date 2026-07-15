"""Verified, accepted-root feature payload loading for inference."""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ContractError, IntegrityError
from .releases import ReleaseManifest, verify_accepted_release
from .schemas import FeatureRow


FEATURE_PAYLOAD = "features.json"


def load_feature_release(
    release_directory: Path,
    *,
    accepted_release_root: Path,
) -> tuple[ReleaseManifest, tuple[FeatureRow, ...]]:
    directory = Path(release_directory)
    manifest = verify_accepted_release(
        directory,
        accepted_root=Path(accepted_release_root),
    )
    if (
        manifest.project != "US_stocks_swing_model_v2"
        or manifest.dataset != "features"
        or manifest.role != "feature_only"
        or manifest.quality_state != "PASS"
    ):
        raise ContractError("inference feature release has the wrong project/dataset/role")
    if FEATURE_PAYLOAD not in {entry.path for entry in manifest.files}:
        raise IntegrityError("feature release lacks the exact features.json payload")
    try:
        payload = json.loads((directory / FEATURE_PAYLOAD).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("feature release payload is missing or invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "rows"}:
        raise IntegrityError("feature release payload fields differ from the exact contract")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise IntegrityError("feature release payload schema is invalid")
    if not isinstance(payload["rows"], list) or len(payload["rows"]) != manifest.row_count:
        raise IntegrityError("feature release row_count differs from its payload")
    rows = tuple(
        FeatureRow.from_release_payload(
            row,
            source_release_id=manifest.release_id,
            source_epoch=manifest.source_epoch,
        )
        for row in payload["rows"]
    )
    identities = [(row.decision_session, row.asset_id) for row in rows]
    if identities != sorted(set(identities)):
        raise IntegrityError("feature release rows must be sorted and unique by session/asset")
    return manifest, rows
