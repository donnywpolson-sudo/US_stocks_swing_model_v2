"""Project-owned evidence contract for external-strategy trial censuses."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping

from .common import canonical_json_bytes, parse_utc_z, require_sha256, sha256_bytes
from .errors import ContractError


MAX_CENSUS_BYTES = 256 * 1024
SOURCE_KINDS = (
    "legacy_repository_trial_records",
    "local_project_trial_records",
    "manual_reports_and_plots",
    "external_outcome_exposure_records",
)
COUNTING_POLICY = {
    "outcome_informed_configurations_count": True,
    "outcome_informed_revisions_count": True,
    "uncertain_attempts_count_conservatively": True,
    "predeclared_cost_curves_are_separate_diagnostics": True,
    "scout_completion_claim_is_authority": False,
}


def _exact_dict(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ContractError(f"{name} fields differ")
    return value


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class CensusSourceEvidence:
    source_kind: str
    locator_sha256: str
    inspected: bool
    outcome_informed_attempt_count: int | None

    def validate(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise ContractError("census source kind is invalid")
        require_sha256(self.locator_sha256, "census source locator")
        if type(self.inspected) is not bool:
            raise ContractError("census source inspected state must be boolean")
        if self.outcome_informed_attempt_count is not None:
            _exact_int(self.outcome_informed_attempt_count, "census source attempt count")
        if not self.inspected and self.outcome_informed_attempt_count is not None:
            raise ContractError("uninspected census source cannot assert a count")

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "source_kind": self.source_kind,
            "locator_sha256": self.locator_sha256,
            "inspected": self.inspected,
            "outcome_informed_attempt_count": self.outcome_informed_attempt_count,
        }


@dataclass(frozen=True)
class HistoricalTrialCensusAssessment:
    payload: Mapping[str, Any]

    @property
    def assessment_id(self) -> str:
        return str(self.payload["assessment_id"])

    @property
    def status(self) -> str:
        return str(self.payload["completion"]["status"])

    @property
    def census_anchor_id(self) -> str:
        return str(self.payload["anchors"]["census_anchor_id"])

    @property
    def trial_family_anchor_id(self) -> str:
        return str(self.payload["anchors"]["trial_family_anchor_id"])

    @property
    def exact_global_count(self) -> int | None:
        return self.payload["counts"]["exact_global_outcome_informed_attempt_count"]

    @property
    def exact_family_count(self) -> int | None:
        return self.payload["counts"]["exact_family_outcome_informed_attempt_count"]

    @classmethod
    def from_bytes(cls, raw: bytes) -> "HistoricalTrialCensusAssessment":
        if not raw or len(raw) > MAX_CENSUS_BYTES:
            raise ContractError("census assessment exceeds bounded size")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("census assessment is not valid UTF-8 JSON") from exc
        root = _exact_dict(
            value,
            {
                "schema_version", "project", "mode", "candidate_spec_id",
                "trial_family_id", "evidence_cutoff_utc", "sources", "counts",
                "counting_policy", "completion", "anchors", "assessment_id",
            },
            "census assessment",
        )
        if (
            root["schema_version"] != 1
            or root["project"] != "US_stocks_swing_model_v2"
            or root["mode"] != "EXTERNAL_STRATEGY_HISTORICAL_TRIAL_CENSUS_ASSESSMENT"
        ):
            raise ContractError("census assessment identity differs")
        require_sha256(root["candidate_spec_id"], "candidate spec ID")
        if type(root["trial_family_id"]) is not str or not root["trial_family_id"].isascii() or not root["trial_family_id"]:
            raise ContractError("census trial family ID is invalid")
        parse_utc_z(root["evidence_cutoff_utc"], "census evidence cutoff")
        if type(root["sources"]) is not list or len(root["sources"]) != len(SOURCE_KINDS):
            raise ContractError("census assessment requires every source kind")
        sources: list[CensusSourceEvidence] = []
        for item in root["sources"]:
            row = _exact_dict(item, {"source_kind", "locator_sha256", "inspected", "outcome_informed_attempt_count"}, "census source")
            source = CensusSourceEvidence(**row)
            source.validate()
            sources.append(source)
        if tuple(item.source_kind for item in sources) != SOURCE_KINDS:
            raise ContractError("census source order differs")
        counts = _exact_dict(
            root["counts"],
            {"documented_prior_floor", "unresolved_attempt_count", "exact_global_outcome_informed_attempt_count", "exact_family_outcome_informed_attempt_count"},
            "census counts",
        )
        floor = _exact_int(counts["documented_prior_floor"], "documented prior floor")
        unresolved = _exact_int(counts["unresolved_attempt_count"], "unresolved attempt count")
        for name in ("exact_global_outcome_informed_attempt_count", "exact_family_outcome_informed_attempt_count"):
            if counts[name] is not None:
                _exact_int(counts[name], name)
        if root["counting_policy"] != COUNTING_POLICY:
            raise ContractError("census counting policy differs")
        completion = _exact_dict(root["completion"], {"all_sources_inspected", "exact_census_complete", "status"}, "census completion")
        complete = all(item.inspected for item in sources) and unresolved == 0 and counts["exact_global_outcome_informed_attempt_count"] is not None and counts["exact_family_outcome_informed_attempt_count"] is not None
        if complete:
            if counts["exact_global_outcome_informed_attempt_count"] < max(floor, counts["exact_family_outcome_informed_attempt_count"]):
                raise ContractError("exact census counts contradict the documented floor or family count")
            expected_completion = {"all_sources_inspected": True, "exact_census_complete": True, "status": "COMPLETE"}
        else:
            expected_completion = {"all_sources_inspected": all(item.inspected for item in sources), "exact_census_complete": False, "status": "INDETERMINATE_BLOCKS_TRUSTED_GATE"}
            if counts["exact_global_outcome_informed_attempt_count"] is not None or counts["exact_family_outcome_informed_attempt_count"] is not None:
                raise ContractError("indeterminate census cannot claim exact counts")
        if completion != expected_completion:
            raise ContractError("census completion state differs from evidence")
        anchors = _exact_dict(root["anchors"], {"census_anchor_id", "trial_family_anchor_id"}, "census anchors")
        anchor_base = {key: item for key, item in root.items() if key not in {"anchors", "assessment_id"}}
        expected_census = sha256_bytes(canonical_json_bytes(anchor_base))
        expected_family = sha256_bytes(canonical_json_bytes({"candidate_spec_id": root["candidate_spec_id"], "trial_family_id": root["trial_family_id"], "counts": counts, "counting_policy": root["counting_policy"], "census_anchor_id": expected_census}))
        if anchors != {"census_anchor_id": expected_census, "trial_family_anchor_id": expected_family}:
            raise ContractError("census anchors differ from content")
        require_sha256(root["assessment_id"], "census assessment ID")
        unsigned = {key: item for key, item in root.items() if key != "assessment_id"}
        if root["assessment_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
            raise ContractError("census assessment ID differs from content")
        return cls(payload=dict(root))


def build_historical_trial_census_assessment(
    *,
    candidate_spec_id: str,
    trial_family_id: str,
    evidence_cutoff_utc: str,
    sources: Iterable[CensusSourceEvidence],
    documented_prior_floor: int,
    unresolved_attempt_count: int,
    exact_global_outcome_informed_attempt_count: int | None,
    exact_family_outcome_informed_attempt_count: int | None,
) -> HistoricalTrialCensusAssessment:
    rows = tuple(sources)
    if tuple(item.source_kind for item in rows) != SOURCE_KINDS:
        raise ContractError("census assessment requires every source in canonical order")
    for item in rows:
        item.validate()
    complete = all(item.inspected for item in rows) and unresolved_attempt_count == 0 and exact_global_outcome_informed_attempt_count is not None and exact_family_outcome_informed_attempt_count is not None
    base = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "mode": "EXTERNAL_STRATEGY_HISTORICAL_TRIAL_CENSUS_ASSESSMENT",
        "candidate_spec_id": candidate_spec_id,
        "trial_family_id": trial_family_id,
        "evidence_cutoff_utc": evidence_cutoff_utc,
        "sources": [item.as_dict() for item in rows],
        "counts": {
            "documented_prior_floor": documented_prior_floor,
            "unresolved_attempt_count": unresolved_attempt_count,
            "exact_global_outcome_informed_attempt_count": exact_global_outcome_informed_attempt_count,
            "exact_family_outcome_informed_attempt_count": exact_family_outcome_informed_attempt_count,
        },
        "counting_policy": dict(COUNTING_POLICY),
        "completion": {
            "all_sources_inspected": all(item.inspected for item in rows),
            "exact_census_complete": complete,
            "status": "COMPLETE" if complete else "INDETERMINATE_BLOCKS_TRUSTED_GATE",
        },
    }
    census_anchor = sha256_bytes(canonical_json_bytes(base))
    anchors = {
        "census_anchor_id": census_anchor,
        "trial_family_anchor_id": sha256_bytes(canonical_json_bytes({"candidate_spec_id": candidate_spec_id, "trial_family_id": trial_family_id, "counts": base["counts"], "counting_policy": base["counting_policy"], "census_anchor_id": census_anchor})),
    }
    unsigned = {**base, "anchors": anchors}
    value = {**unsigned, "assessment_id": sha256_bytes(canonical_json_bytes(unsigned))}
    return HistoricalTrialCensusAssessment.from_bytes(canonical_json_bytes(value))
