"""Explicit capabilities for mechanics-only synthetic construction.

Synthetic permits are content-addressed labels, not production authority. Any
artifact created with one must remain outside real-history, candidate, and
prospective evidence paths.
"""

from __future__ import annotations

from dataclasses import dataclass

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError


SYNTHETIC_PROJECT = "US_stocks_swing_model_v2"


@dataclass(frozen=True)
class SyntheticOnlyPermit:
    schema_version: int
    project: str
    fixture_id: str
    scope: str
    permit_id: str

    @classmethod
    def create(cls, *, fixture_id: str, scope: str) -> "SyntheticOnlyPermit":
        unsigned = {
            "schema_version": 1,
            "project": SYNTHETIC_PROJECT,
            "fixture_id": fixture_id,
            "scope": scope,
        }
        permit = cls(
            **unsigned,
            permit_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        permit.validate(scope)
        return permit

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "fixture_id": self.fixture_id,
            "scope": self.scope,
        }

    def validate(self, expected_scope: str) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.project != SYNTHETIC_PROJECT
            or not self.fixture_id
            or self.scope != expected_scope
        ):
            raise ContractError("synthetic-only permit scope/project is invalid")
        require_sha256(self.permit_id, "synthetic_only.permit_id")
        if self.permit_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("synthetic-only permit ID differs from its content")


def require_synthetic_permit(
    permit: SyntheticOnlyPermit | None,
    *,
    scope: str,
) -> SyntheticOnlyPermit:
    if permit is None:
        raise ContractError(f"{scope} requires an explicit synthetic-only permit")
    permit.validate(scope)
    return permit
