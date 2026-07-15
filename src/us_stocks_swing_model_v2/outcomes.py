from __future__ import annotations

import math
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

from .calendar import PinnedSessionCalendar
from .common import parse_timestamp, require_aware_utc, require_sha256
from .corporate_actions import ActionType, BitemporalActionLedger
from .errors import ContractError
from .schemas import OutcomeRow, OutcomeStatus
from .releases import ReleaseManifest, verify_accepted_release


@dataclass(frozen=True)
class DailyBar:
    asset_id: str
    session: date
    open: float | None
    close: float | None
    available_at: datetime
    halted: bool = False
    delisted: bool = False

    def validate(self) -> None:
        require_aware_utc(self.available_at, "available_at")
        for name, value in (("open", self.open), ("close", self.close)):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ContractError(f"{name} must be positive and finite")
        if type(self.halted) is not bool or type(self.delisted) is not bool:
            raise ContractError("bar halted/delisted flags must be boolean")


def load_daily_bar_release(
    release_directory: Path,
    *,
    accepted_release_root: Path,
) -> tuple[ReleaseManifest, tuple[DailyBar, ...]]:
    directory = Path(release_directory)
    manifest = verify_accepted_release(directory, accepted_root=Path(accepted_release_root))
    if (
        manifest.project != "US_stocks_swing_model_v2"
        or manifest.dataset != "bars"
        or manifest.role not in {"active_historical", "prospective_as_received"}
        or manifest.quality_state != "PASS"
    ):
        raise ContractError("outcome bar release has the wrong project/dataset/role")
    try:
        payload = json.loads((directory / "daily_bars.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("daily-bar release payload is missing or invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "rows"}:
        raise ContractError("daily-bar release payload fields differ")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ContractError("daily-bar release payload schema is invalid")
    if not isinstance(payload["rows"], list) or len(payload["rows"]) != manifest.row_count:
        raise ContractError("daily-bar release row_count differs from payload")
    expected = {"asset_id", "session", "open", "close", "available_at", "halted", "delisted"}
    bars: list[DailyBar] = []
    for raw in payload["rows"]:
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ContractError("daily-bar payload row fields differ")
        for name in ("open", "close"):
            if raw[name] is not None and (
                isinstance(raw[name], bool) or not isinstance(raw[name], (int, float))
            ):
                raise ContractError("daily-bar prices must be numeric or null")
        if type(raw["halted"]) is not bool or type(raw["delisted"]) is not bool:
            raise ContractError("daily-bar flags must be boolean")
        bar = DailyBar(
            asset_id=str(raw["asset_id"]),
            session=date.fromisoformat(str(raw["session"])),
            open=(float(raw["open"]) if raw["open"] is not None else None),
            close=(float(raw["close"]) if raw["close"] is not None else None),
            available_at=parse_timestamp(str(raw["available_at"]), "bar.available_at"),
            halted=raw["halted"],
            delisted=raw["delisted"],
        )
        bar.validate()
        bars.append(bar)
    identities = [(bar.asset_id, bar.session) for bar in bars]
    if identities != sorted(set(identities)):
        raise ContractError("daily-bar release rows must be sorted and unique")
    return manifest, tuple(bars)


def build_outcome(
    *,
    prediction_id: str,
    eligibility_census_id: str,
    asset_id: str,
    decision_session: date,
    calendar: PinnedSessionCalendar,
    bars: Mapping[date, DailyBar],
    bar_release_id: str,
    actions: BitemporalActionLedger,
    action_view_as_of: datetime,
    source_epoch: str,
    revision_number: int = 1,
    prior_revision_id: str | None = None,
) -> OutcomeRow:
    as_of = require_aware_utc(action_view_as_of, "action_view_as_of")
    if calendar.trust_eligible != actions.trust_eligible:
        raise ContractError("calendar and corporate-action evidence trust modes differ")
    if not source_epoch:
        raise ContractError("outcome source_epoch is required")
    require_sha256(bar_release_id, "outcome.bar_release_id")
    horizon = calendar.outcome_sessions(decision_session)
    if horizon is None:
        return _make_outcome(
            prediction_id=prediction_id,
            eligibility_census_id=eligibility_census_id,
            asset_id=asset_id,
            decision_session=decision_session,
            entry_session=None,
            exit_session=None,
            status=OutcomeStatus.PENDING,
            realized=None,
            reason="fifth outcome session has not entered the pinned calendar",
            calendar=calendar,
            bar_release_id=bar_release_id,
            action_release_id=actions.release_id,
            source_epoch=source_epoch,
            as_of=as_of,
            revision_number=revision_number,
            prior_revision_id=prior_revision_id,
        )
    entry_session, exit_session = horizon
    interval = calendar.interval(entry_session, exit_session)
    interval_set = set(interval)
    for mapping_session, bar in bars.items():
        if mapping_session != bar.session:
            raise ContractError("bar mapping key must equal bar.session")
        bar.validate()
        if bar.asset_id != asset_id or mapping_session not in interval_set:
            raise ContractError("bar identity/session does not match outcome request")
    available_bars = [bars.get(session) for session in interval]
    if any(bar and bar.delisted for bar in available_bars):
        return _unresolved(
            prediction_id, eligibility_census_id, asset_id, decision_session, entry_session, exit_session,
            OutcomeStatus.DELISTED, "asset delisted during outcome interval", calendar,
            bar_release_id, actions.release_id, source_epoch, as_of,
            revision_number, prior_revision_id,
        )
    entry_bar = bars.get(entry_session)
    exit_bar = bars.get(exit_session)
    if (entry_bar and entry_bar.halted and entry_bar.open is None) or (exit_bar and exit_bar.halted and exit_bar.close is None):
        return _unresolved(
            prediction_id, eligibility_census_id, asset_id, decision_session, entry_session, exit_session,
            OutcomeStatus.HALTED, "required execution price unavailable due to halt", calendar,
            bar_release_id, actions.release_id, source_epoch, as_of,
            revision_number, prior_revision_id,
        )
    if entry_bar is None or exit_bar is None or entry_bar.open is None or exit_bar.close is None:
        return _unresolved(
            prediction_id, eligibility_census_id, asset_id, decision_session, entry_session, exit_session,
            OutcomeStatus.MISSING_SOURCE, "required entry-open or exit-close source bar is missing",
            calendar, bar_release_id, actions.release_id, source_epoch, as_of,
            revision_number, prior_revision_id,
        )
    if as_of < max(
        require_aware_utc(entry_bar.available_at, "entry_available_at"),
        require_aware_utc(exit_bar.available_at, "exit_available_at"),
    ):
        raise ContractError("action view cannot predate required outcome-bar availability")
    relevant_actions = actions.effective_between(asset_id, entry_session, exit_session, as_of)
    unsupported = [
        action for action in relevant_actions
        if action.action_type not in {ActionType.SPLIT, ActionType.DIVIDEND}
    ]
    if unsupported:
        return _unresolved(
            prediction_id, eligibility_census_id, asset_id, decision_session, entry_session, exit_session,
            OutcomeStatus.ACTION_UNRESOLVED,
            "non-split corporate action prevents a comparable price-return outcome",
            calendar, bar_release_id, actions.release_id, source_epoch, as_of,
            revision_number, prior_revision_id,
        )
    split_multiple = math.prod(
        action.ratio_new_for_old or 1.0
        for action in relevant_actions
        if action.action_type is ActionType.SPLIT
    )
    realized = (exit_bar.close * split_multiple / entry_bar.open) - 1.0
    return _make_outcome(
        prediction_id=prediction_id,
        eligibility_census_id=eligibility_census_id,
        asset_id=asset_id,
        decision_session=decision_session,
        entry_session=entry_session,
        exit_session=exit_session,
        status=OutcomeStatus.MATURED,
        realized=realized,
        reason=None,
        calendar=calendar,
        bar_release_id=bar_release_id,
        action_release_id=actions.release_id,
        source_epoch=source_epoch,
        as_of=as_of,
        revision_number=revision_number,
        prior_revision_id=prior_revision_id,
    )


def _unresolved(
    prediction_id: str,
    eligibility_census_id: str,
    asset_id: str,
    decision_session: date,
    entry_session: date,
    exit_session: date,
    status: OutcomeStatus,
    reason: str,
    calendar: PinnedSessionCalendar,
    bar_release_id: str,
    action_release_id: str,
    source_epoch: str,
    as_of: datetime,
    revision_number: int,
    prior_revision_id: str | None,
) -> OutcomeRow:
    return _make_outcome(
        prediction_id=prediction_id,
        eligibility_census_id=eligibility_census_id,
        asset_id=asset_id,
        decision_session=decision_session,
        entry_session=entry_session,
        exit_session=exit_session,
        status=status,
        realized=None,
        reason=reason,
        calendar=calendar,
        bar_release_id=bar_release_id,
        action_release_id=action_release_id,
        source_epoch=source_epoch,
        as_of=as_of,
        revision_number=revision_number,
        prior_revision_id=prior_revision_id,
    )


def _make_outcome(
    *,
    prediction_id: str,
    eligibility_census_id: str,
    asset_id: str,
    decision_session: date,
    entry_session: date | None,
    exit_session: date | None,
    status: OutcomeStatus,
    realized: float | None,
    reason: str | None,
    calendar: PinnedSessionCalendar,
    bar_release_id: str,
    action_release_id: str,
    source_epoch: str,
    as_of: datetime,
    revision_number: int,
    prior_revision_id: str | None,
) -> OutcomeRow:
    return OutcomeRow.create(
        prediction_id=prediction_id,
        eligibility_census_id=eligibility_census_id,
        revision_number=revision_number,
        prior_revision_id=prior_revision_id,
        asset_id=asset_id,
        decision_session=decision_session,
        entry_session=entry_session,
        exit_session=exit_session,
        status=status,
        split_normalized_price_return=realized,
        reason=reason,
        calendar_release_id=calendar.release_id,
        bar_release_id=bar_release_id,
        action_release_id=action_release_id,
        source_epoch=source_epoch,
        action_view_as_of=as_of,
        target_semantics="SIMPLE_SPLIT_NORMALIZED_PRICE_RETURN",
    )
