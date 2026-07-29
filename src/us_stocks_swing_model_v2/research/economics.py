"""Independent synthetic reconstruction of the frozen five-cohort economics."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Mapping

from .contracts import ResearchContractError


REQUIRED_SLEEVES = ("stock_long", "stock_short", "etf_long", "etf_short")
CAPACITY_STATES = {"PASS", "FAIL", "UNKNOWN"}
BORROW_STATES = {"NOT_APPLICABLE", "GROSS_ONLY_BORROW_EXCLUDED"}


def _canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ResearchContractError(
            f"value is not canonical-JSON serializable: {exc}"
        ) from exc
    return encoded.encode("utf-8") + b"\n"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ResearchContractError(f"{field} must be exact lowercase SHA-256")
    return value


def _finite_float(value: object, field: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ResearchContractError(f"{field} must be an exact finite float")
    return value


@dataclass(frozen=True)
class EconomicPolicy:
    concurrent_cohorts: int = 5
    capital_fraction_per_new_cohort: float = 0.2
    one_way_cost_basis_points: tuple[int, ...] = (0, 10, 25, 50)
    binding_stress_cost_basis_points: int = 25
    portfolio_notional: float | None = None
    maximum_adv_participation: float | None = None
    borrow_cost_mode: str = "EXCLUDED_BY_SCOPE"

    def validate(self) -> None:
        if type(self.concurrent_cohorts) is not int or self.concurrent_cohorts != 5:
            raise ResearchContractError("economic policy requires exactly five cohorts")
        fraction = _finite_float(
            self.capital_fraction_per_new_cohort,
            "capital_fraction_per_new_cohort",
        )
        if fraction != 0.2:
            raise ResearchContractError("each new cohort must receive one-fifth capital")
        if (
            type(self.one_way_cost_basis_points) is not tuple
            or self.one_way_cost_basis_points != (0, 10, 25, 50)
            or self.binding_stress_cost_basis_points != 25
        ):
            raise ResearchContractError("economic cost schedule differs from the frozen contract")
        if (self.portfolio_notional is None) != (
            self.maximum_adv_participation is None
        ):
            raise ResearchContractError("capacity notional and ADV limit must be supplied together")
        if self.portfolio_notional is not None:
            notional = _finite_float(self.portfolio_notional, "portfolio_notional")
            participation = _finite_float(
                self.maximum_adv_participation,
                "maximum_adv_participation",
            )
            if notional <= 0.0 or not 0.0 < participation <= 1.0:
                raise ResearchContractError("capacity contract values are invalid")
        if self.borrow_cost_mode != "EXCLUDED_BY_SCOPE":
            raise ResearchContractError("borrow cost mode differs from the frozen scope")


@dataclass(frozen=True)
class DailyCohortBook:
    session: int
    cohort_weights: Mapping[str, Mapping[str, float]]
    cohort_sleeves: Mapping[str, str]
    asset_returns: Mapping[str, float | None]
    asset_adv_notional: Mapping[str, float | None]

    def validate(self) -> None:
        if type(self.session) is not int or self.session < 1:
            raise ResearchContractError("book session must be a positive integer")
        for value, name in (
            (self.cohort_weights, "cohort_weights"),
            (self.cohort_sleeves, "cohort_sleeves"),
            (self.asset_returns, "asset_returns"),
            (self.asset_adv_notional, "asset_adv_notional"),
        ):
            if type(value) is not dict:
                raise ResearchContractError(f"{name} must be an exact dict")
        if set(self.cohort_weights) != set(self.cohort_sleeves):
            raise ResearchContractError("cohort weights and sleeves differ")
        if len(self.cohort_weights) > 5:
            raise ResearchContractError("more than five cohorts are active")
        for cohort_id, weights in self.cohort_weights.items():
            if type(cohort_id) is not str or not cohort_id:
                raise ResearchContractError("cohort ID must be nonempty text")
            if type(weights) is not dict or not weights:
                raise ResearchContractError("cohort weights must be an exact nonempty dict")
            sleeve = self.cohort_sleeves[cohort_id]
            if sleeve not in REQUIRED_SLEEVES:
                raise ResearchContractError("cohort sleeve is not one of the four frozen sleeves")
            gross = 0.0
            for asset_id, weight in weights.items():
                if type(asset_id) is not str or not asset_id:
                    raise ResearchContractError("asset ID must be nonempty text")
                checked_weight = _finite_float(
                    weight,
                    f"weight[{cohort_id},{asset_id}]",
                )
                if sleeve.endswith("_long") and checked_weight < 0.0:
                    raise ResearchContractError("long sleeve contains a short weight")
                if sleeve.endswith("_short") and checked_weight > 0.0:
                    raise ResearchContractError("short sleeve contains a long weight")
                gross += abs(checked_weight)
            if gross > 0.2 + 1e-12:
                raise ResearchContractError("one cohort exceeds one-fifth capital")
        for asset_id, value in self.asset_returns.items():
            if type(asset_id) is not str or not asset_id:
                raise ResearchContractError("return asset ID must be nonempty text")
            if value is not None:
                _finite_float(value, f"asset_return[{asset_id}]")
        for asset_id, value in self.asset_adv_notional.items():
            if type(asset_id) is not str or not asset_id:
                raise ResearchContractError("ADV asset ID must be nonempty text")
            if value is not None and _finite_float(value, f"asset_adv[{asset_id}]") <= 0.0:
                raise ResearchContractError("ADV notional must be positive")


@dataclass(frozen=True)
class DailyEconomicRow:
    session: int
    gross_return: float | None
    turnover: float
    net_returns: tuple[tuple[int, float | None], ...]
    outcome_status: str


@dataclass(frozen=True)
class EconomicReconstruction:
    rows: tuple[DailyEconomicRow, ...]
    capacity_status: str
    borrow_status: str
    required_sleeves: tuple[str, ...]
    mechanics_only: bool
    reconstruction_id: str


def reconstruct_five_cohort_economics(
    books: tuple[DailyCohortBook, ...],
    *,
    policy: EconomicPolicy = EconomicPolicy(),
) -> EconomicReconstruction:
    """Rebuild actual-weight turnover and net returns without alpha authority."""

    policy.validate()
    if type(books) is not tuple or not books:
        raise ResearchContractError("economic reconstruction requires an exact nonempty tuple")
    sessions = tuple(book.session for book in books)
    if any(current <= previous for previous, current in zip(sessions, sessions[1:])):
        raise ResearchContractError("economic sessions must be strictly increasing")
    for book in books:
        if type(book) is not DailyCohortBook:
            raise ResearchContractError("economic inputs must be exact DailyCohortBook values")
        book.validate()

    cohort_sessions: dict[str, list[int]] = {}
    cohort_sleeves: dict[str, str] = {}
    for book in books:
        for cohort_id, sleeve in book.cohort_sleeves.items():
            cohort_sessions.setdefault(cohort_id, []).append(book.session)
            prior = cohort_sleeves.setdefault(cohort_id, sleeve)
            if prior != sleeve:
                raise ResearchContractError("cohort sleeve changed during its life")
    if set(cohort_sleeves.values()) != set(REQUIRED_SLEEVES):
        raise ResearchContractError("economic reconstruction must cover all four sleeves")
    starts = [values[0] for values in cohort_sessions.values()]
    if len(starts) != len(set(starts)):
        raise ResearchContractError("at most one new cohort may start per session")
    for values in cohort_sessions.values():
        if len(values) != 5 or values != list(range(values[0], values[0] + 5)):
            raise ResearchContractError("each cohort must span exactly five pinned session ordinals")

    previous_weights: dict[str, float] = {}
    rows: list[DailyEconomicRow] = []
    capacity_unknown = policy.portfolio_notional is None
    capacity_failed = False
    short_present = False
    for book in books:
        total_weights: dict[str, float] = {}
        for weights in book.cohort_weights.values():
            for asset_id, weight in weights.items():
                total_weights[asset_id] = total_weights.get(asset_id, 0.0) + weight
                short_present = short_present or weight < 0.0
        assets = set(previous_weights) | set(total_weights)
        turnover = float(
            sum(abs(total_weights.get(asset, 0.0) - previous_weights.get(asset, 0.0)) for asset in assets)
        )
        if not math.isfinite(turnover):
            raise ResearchContractError("economic turnover is non-finite")

        missing = [
            asset
            for asset, weight in total_weights.items()
            if abs(weight) > 0.0
            and (
                asset not in book.asset_returns
                or book.asset_returns[asset] is None
            )
        ]
        if missing:
            gross_return = None
            status = "UNAVAILABLE_RETURN"
        else:
            gross_return = float(
                sum(
                    weight * float(book.asset_returns[asset])
                    for asset, weight in total_weights.items()
                )
            )
            if not math.isfinite(gross_return):
                raise ResearchContractError("economic gross return is non-finite")
            status = "AVAILABLE"

        net_returns: list[tuple[int, float | None]] = []
        for cost_bps in policy.one_way_cost_basis_points:
            net = (
                None
                if gross_return is None
                else gross_return - turnover * (cost_bps / 10_000.0)
            )
            net_returns.append((cost_bps, net))
        finite_net = [value for _, value in net_returns if value is not None]
        if any(later > earlier + 1e-15 for earlier, later in zip(finite_net, finite_net[1:])):
            raise ResearchContractError("economic costs are not monotonic")

        if policy.portfolio_notional is not None:
            for asset in assets:
                delta = abs(total_weights.get(asset, 0.0) - previous_weights.get(asset, 0.0))
                if delta == 0.0:
                    continue
                adv = book.asset_adv_notional.get(asset)
                if adv is None:
                    capacity_unknown = True
                    continue
                traded = delta * policy.portfolio_notional
                if traded > adv * policy.maximum_adv_participation + 1e-12:
                    capacity_failed = True
        rows.append(
            DailyEconomicRow(
                session=book.session,
                gross_return=gross_return,
                turnover=turnover,
                net_returns=tuple(net_returns),
                outcome_status=status,
            )
        )
        previous_weights = total_weights

    capacity_status = (
        "FAIL" if capacity_failed else "UNKNOWN" if capacity_unknown else "PASS"
    )
    borrow_status = (
        "GROSS_ONLY_BORROW_EXCLUDED" if short_present else "NOT_APPLICABLE"
    )
    unsigned = {
        "rows": [
            {
                "session": row.session,
                "gross_return": row.gross_return,
                "turnover": row.turnover,
                "net_returns": [
                    {"cost_bps": cost, "net_return": value}
                    for cost, value in row.net_returns
                ],
                "outcome_status": row.outcome_status,
            }
            for row in rows
        ],
        "capacity_status": capacity_status,
        "borrow_status": borrow_status,
        "required_sleeves": list(REQUIRED_SLEEVES),
        "mechanics_only": True,
    }
    result = EconomicReconstruction(
        rows=tuple(rows),
        capacity_status=capacity_status,
        borrow_status=borrow_status,
        required_sleeves=REQUIRED_SLEEVES,
        mechanics_only=True,
        reconstruction_id=_sha256_bytes(_canonical_json_bytes(unsigned)),
    )
    if result.capacity_status not in CAPACITY_STATES or result.borrow_status not in BORROW_STATES:
        raise ResearchContractError("economic reconstruction state is invalid")
    _require_sha256(result.reconstruction_id, "economic.reconstruction_id")
    return result
