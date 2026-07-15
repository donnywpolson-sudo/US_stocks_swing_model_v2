from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .common import require_sha256
from .errors import ContractError


@dataclass(frozen=True, init=False)
class PinnedSessionCalendar:
    release_id: str
    sessions: tuple[date, ...]
    verification_state: str
    verification_receipt_id: str

    @classmethod
    def _construct(cls, **fields: object) -> "PinnedSessionCalendar":
        value = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(value, name, fields[name])
        return value

    @classmethod
    def from_iso_dates(
        cls,
        release_id: str,
        values: Iterable[str],
        *,
        synthetic_permit: SyntheticOnlyPermit,
    ) -> "PinnedSessionCalendar":
        permit = require_synthetic_permit(
            synthetic_permit,
            scope="SYNTHETIC_SESSION_CALENDAR",
        )
        try:
            sessions = tuple(date.fromisoformat(value) for value in values)
        except ValueError as exc:
            raise ContractError("calendar contains an invalid ISO session") from exc
        if release_id != permit.permit_id:
            raise ContractError("synthetic calendar release_id must equal its permit ID")
        if not sessions or list(sessions) != sorted(set(sessions)):
            raise ContractError("calendar sessions must be nonempty, sorted, and unique")
        return cls._construct(
            release_id=release_id,
            sessions=sessions,
            verification_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
            verification_receipt_id=permit.permit_id,
        )

    @classmethod
    def _from_verified_release_payload(
        cls,
        *,
        release_id: str,
        sessions: Iterable[date],
        verification_receipt_id: str,
    ) -> "PinnedSessionCalendar":
        ordered = tuple(sessions)
        require_sha256(release_id, "calendar.release_id")
        require_sha256(verification_receipt_id, "calendar.verification_receipt_id")
        if not ordered or list(ordered) != sorted(set(ordered)):
            raise ContractError("verified calendar sessions must be nonempty, sorted, and unique")
        return cls._construct(
            release_id=release_id,
            sessions=ordered,
            verification_state="VERIFIED_XNYS_RELEASE",
            verification_receipt_id=verification_receipt_id,
        )

    @property
    def trust_eligible(self) -> bool:
        return self.verification_state == "VERIFIED_XNYS_RELEASE"

    def position(self, session: date) -> int:
        try:
            return self.sessions.index(session)
        except ValueError as exc:
            raise ContractError(f"session is not in pinned calendar: {session}") from exc

    def outcome_sessions(self, decision_session: date) -> tuple[date, date] | None:
        """Return next-session open (D1) and fifth-session close (D5)."""
        position = self.position(decision_session)
        if position + 5 >= len(self.sessions):
            return None
        return self.sessions[position + 1], self.sessions[position + 5]

    def interval(self, start: date, end: date) -> tuple[date, ...]:
        start_position = self.position(start)
        end_position = self.position(end)
        if end_position < start_position:
            raise ContractError("calendar interval is reversed")
        return self.sessions[start_position : end_position + 1]
