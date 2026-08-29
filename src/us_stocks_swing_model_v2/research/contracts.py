"""Fail-closed contracts for synthetic research mechanics.

This package deliberately contains no real-history authorization.  A synthetic
permit proves only that a caller explicitly labelled an in-memory fixture as
synthetic; it is not evidence of alpha and cannot authorize candidate sealing.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from numbers import Real
import re
from typing import Iterable

import numpy as np


class ResearchContractError(ValueError):
    """Raised when a research-mechanics contract fails closed."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def explicit_real(value: object, *, name: str) -> float:
    """Accept a finite scalar real while rejecting bool and coercible strings."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ResearchContractError(f"{name} must be an explicit real scalar")
    result = float(value)
    if not np.isfinite(result):
        raise ResearchContractError(f"{name} must be finite")
    return result


def explicit_int(value: object, *, name: str) -> int:
    """Accept an integral scalar while rejecting Python/NumPy booleans."""

    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ResearchContractError(f"{name} must be an explicit integer")
    return int(value)


def finite_float64(
    value: np.ndarray,
    *,
    name: str,
    ndim: int | None = None,
) -> np.ndarray:
    """Return ``value`` only when it is an exact, finite float64 array.

    Values are never cast and non-finite rows are never dropped.
    """

    if not isinstance(value, np.ndarray):
        raise ResearchContractError(f"{name} must be a numpy.ndarray")
    if value.dtype != np.dtype(np.float64):
        raise ResearchContractError(f"{name} must have dtype float64")
    if ndim is not None and value.ndim != ndim:
        raise ResearchContractError(f"{name} must have ndim={ndim}")
    if value.size == 0:
        raise ResearchContractError(f"{name} must be non-empty")
    if not bool(np.all(np.isfinite(value))):
        raise ResearchContractError(f"{name} contains NaN or infinity")
    return value


def int64_vector(value: np.ndarray, *, name: str) -> np.ndarray:
    if not isinstance(value, np.ndarray):
        raise ResearchContractError(f"{name} must be a numpy.ndarray")
    if value.dtype != np.dtype(np.int64) or value.ndim != 1:
        raise ResearchContractError(f"{name} must be a one-dimensional int64 array")
    if value.size == 0:
        raise ResearchContractError(f"{name} must be non-empty")
    return value


def array_sha256(value: np.ndarray) -> str:
    """Hash shape, dtype, and C-order bytes without mutating the input."""

    header = f"{value.dtype.str}|{value.shape}".encode("ascii")
    payload = np.ascontiguousarray(value).tobytes(order="C")
    return hashlib.sha256(header + b"\0" + payload).hexdigest()


@dataclass(frozen=True)
class SyntheticOnlyPermit:
    purpose: str
    source_kind: str
    generator_id: str
    seed: int
    dataset_sha256: str
    real_history_authorized: bool = False
    candidate_sealing_authorized: bool = False

    def validate(self) -> None:
        if self.purpose != "MECHANICS_ONLY":
            raise ResearchContractError("permit purpose must be MECHANICS_ONLY")
        if self.source_kind != "SYNTHETIC":
            raise ResearchContractError("only SYNTHETIC source_kind is supported")
        if not self.generator_id or not self.generator_id.isascii():
            raise ResearchContractError("generator_id must be non-empty ASCII")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ResearchContractError("seed must be an integer")
        if not (0 <= self.seed < 2**64):
            raise ResearchContractError("seed must fit uint64")
        if not _SHA256_RE.fullmatch(self.dataset_sha256):
            raise ResearchContractError("dataset_sha256 must be lowercase SHA-256")
        if self.real_history_authorized or self.candidate_sealing_authorized:
            raise ResearchContractError(
                "the synthetic package cannot authorize history or candidate sealing"
            )


def make_synthetic_permit(
    fixture: np.ndarray,
    *,
    generator_id: str,
    seed: int,
    source_kind: str = "SYNTHETIC",
) -> SyntheticOnlyPermit:
    finite_float64(fixture, name="fixture")
    permit = SyntheticOnlyPermit(
        purpose="MECHANICS_ONLY",
        source_kind=source_kind,
        generator_id=generator_id,
        seed=seed,
        dataset_sha256=array_sha256(fixture),
    )
    permit.validate()
    return permit


def require_synthetic_permit(
    permit: SyntheticOnlyPermit,
    fixture: np.ndarray | None = None,
) -> None:
    if not isinstance(permit, SyntheticOnlyPermit):
        raise ResearchContractError("a SyntheticOnlyPermit is required")
    permit.validate()
    if fixture is not None:
        finite_float64(fixture, name="fixture")
        if array_sha256(fixture) != permit.dataset_sha256:
            raise ResearchContractError("permit does not bind this exact fixture")


def assert_disjoint_partitions(
    fit_indices: np.ndarray,
    audit_indices: np.ndarray,
    *additional_partitions: np.ndarray,
) -> None:
    """Require non-empty, internally unique, pairwise-disjoint partitions."""

    partitions = (fit_indices, audit_indices, *additional_partitions)
    seen: set[int] = set()
    for number, partition in enumerate(partitions):
        values = int64_vector(partition, name=f"partition_{number}")
        as_set = {int(item) for item in values.tolist()}
        if len(as_set) != len(values):
            raise ResearchContractError(f"partition_{number} contains duplicates")
        overlap = seen.intersection(as_set)
        if overlap:
            raise ResearchContractError(
                f"fit/audit partitions overlap at {sorted(overlap)[:5]}"
            )
        seen.update(as_set)


def require_unique_ascii_ids(values: Iterable[str], *, name: str) -> tuple[str, ...]:
    result = tuple(values)
    if not result:
        raise ResearchContractError(f"{name} must be non-empty")
    if any(not isinstance(value, str) or not value or not value.isascii() for value in result):
        raise ResearchContractError(f"{name} must contain non-empty ASCII strings")
    if len(set(result)) != len(result):
        raise ResearchContractError(f"{name} must be unique")
    return result


@dataclass(frozen=True)
class ResearchArrayBinding:
    """Content binding for a complete trial and multiplicity-family array set.

    This is a prerequisite contract only; it does not authorize real-history
    evaluation or candidate sealing.
    """

    trial_id: str
    trial_family_id: str
    trial_family_anchor_id: str
    census_anchor_id: str
    evaluator_closure_hash: str
    data_release_ids: tuple[str, ...]
    sample_ids_hash: str
    array_hashes: tuple[tuple[str, str], ...]
    binding_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "trial_family_id": self.trial_family_id,
            "trial_family_anchor_id": self.trial_family_anchor_id,
            "census_anchor_id": self.census_anchor_id,
            "evaluator_closure_hash": self.evaluator_closure_hash,
            "data_release_ids": list(self.data_release_ids),
            "sample_ids_hash": self.sample_ids_hash,
            "array_hashes": [[name, digest] for name, digest in self.array_hashes],
        }

    def validate(self) -> None:
        for name in (
            "trial_id",
            "trial_family_anchor_id",
            "census_anchor_id",
            "evaluator_closure_hash",
            "sample_ids_hash",
            "binding_id",
        ):
            if not _SHA256_RE.fullmatch(getattr(self, name)):
                raise ResearchContractError(f"{name} must be lowercase SHA-256")
        if not self.trial_family_id or not self.trial_family_id.isascii():
            raise ResearchContractError("trial_family_id must be nonempty ASCII")
        if list(self.data_release_ids) != sorted(set(self.data_release_ids)) or not self.data_release_ids:
            raise ResearchContractError("data_release_ids must be nonempty, sorted, and unique")
        if any(not _SHA256_RE.fullmatch(value) for value in self.data_release_ids):
            raise ResearchContractError("data release IDs must be lowercase SHA-256")
        names = [name for name, _ in self.array_hashes]
        if not names or names != sorted(set(names)):
            raise ResearchContractError("array binding names must be nonempty, sorted, and unique")
        if any(not name.isascii() or not _SHA256_RE.fullmatch(digest) for name, digest in self.array_hashes):
            raise ResearchContractError("array binding entries are invalid")
        encoded = json.dumps(
            self.unsigned_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        if self.binding_id != hashlib.sha256(encoded).hexdigest():
            raise ResearchContractError("research array binding ID differs from its content")

    @classmethod
    def create(
        cls,
        *,
        trial_id: str,
        trial_family_id: str,
        trial_family_anchor_id: str,
        census_anchor_id: str,
        evaluator_closure_hash: str,
        data_release_ids: Iterable[str],
        sample_ids: Iterable[str],
        arrays: dict[str, np.ndarray],
    ) -> "ResearchArrayBinding":
        ordered_sample_ids = require_unique_ascii_ids(sample_ids, name="sample_ids")
        sample_ids_hash = hashlib.sha256(
            json.dumps(
                list(ordered_sample_ids),
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()
        array_hashes = tuple(
            sorted((name, array_sha256(finite_float64(value, name=name))) for name, value in arrays.items())
        )
        unsigned = {
            "trial_id": trial_id,
            "trial_family_id": trial_family_id,
            "trial_family_anchor_id": trial_family_anchor_id,
            "census_anchor_id": census_anchor_id,
            "evaluator_closure_hash": evaluator_closure_hash,
            "data_release_ids": sorted(set(data_release_ids)),
            "sample_ids_hash": sample_ids_hash,
            "array_hashes": [[name, digest] for name, digest in array_hashes],
        }
        binding = cls(
            trial_id=trial_id,
            trial_family_id=trial_family_id,
            trial_family_anchor_id=trial_family_anchor_id,
            census_anchor_id=census_anchor_id,
            evaluator_closure_hash=evaluator_closure_hash,
            data_release_ids=tuple(unsigned["data_release_ids"]),
            sample_ids_hash=sample_ids_hash,
            array_hashes=array_hashes,
            binding_id=hashlib.sha256(
                json.dumps(
                    unsigned,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest(),
        )
        binding.validate()
        return binding

    def validate_inputs(
        self,
        *,
        sample_ids: Iterable[str],
        arrays: dict[str, np.ndarray],
    ) -> None:
        self.validate()
        ordered_sample_ids = require_unique_ascii_ids(sample_ids, name="sample_ids")
        sample_ids_hash = hashlib.sha256(
            json.dumps(
                list(ordered_sample_ids),
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()
        if sample_ids_hash != self.sample_ids_hash:
            raise ResearchContractError("sample IDs differ from the frozen trial/family binding")
        actual = tuple(
            sorted((name, array_sha256(finite_float64(value, name=name))) for name, value in arrays.items())
        )
        if actual != self.array_hashes:
            raise ResearchContractError("statistical arrays differ from the frozen trial/family binding")
