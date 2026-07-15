"""Repository-issued observation clock capability."""

from __future__ import annotations

from datetime import datetime, timezone

from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .common import require_aware_utc
from .errors import ContractError


_CLOCK_TOKEN = object()


class TrustedClock:
    """Clock values are derived internally; callers cannot pass timestamps.

    The production constructor has no timestamp argument. Fixed time is
    available only through an explicit mechanics-only synthetic permit.
    """

    __slots__ = ("_mode", "_fixed_at", "_synthetic_permit_id")

    def __init__(
        self,
        token: object,
        *,
        mode: str,
        fixed_at: datetime | None,
        synthetic_permit_id: str | None,
    ) -> None:
        if token is not _CLOCK_TOKEN:
            raise ContractError("TrustedClock must be created by a repository-issued factory")
        self._mode = mode
        self._fixed_at = fixed_at
        self._synthetic_permit_id = synthetic_permit_id

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("TrustedClock cannot be subclassed")

    @classmethod
    def production(cls) -> "TrustedClock":
        return cls(
            _CLOCK_TOKEN,
            mode="PRODUCTION_SYSTEM_UTC",
            fixed_at=None,
            synthetic_permit_id=None,
        )

    @classmethod
    def synthetic_fixed(
        cls,
        fixed_at: datetime,
        *,
        permit: SyntheticOnlyPermit,
    ) -> "TrustedClock":
        verified = require_synthetic_permit(permit, scope="TRUSTED_CLOCK_FIXED_TIME")
        return cls(
            _CLOCK_TOKEN,
            mode="SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
            fixed_at=require_aware_utc(fixed_at, "synthetic_clock.fixed_at"),
            synthetic_permit_id=verified.permit_id,
        )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def trust_eligible(self) -> bool:
        return self._mode == "PRODUCTION_SYSTEM_UTC"

    @property
    def synthetic_permit_id(self) -> str | None:
        return self._synthetic_permit_id

    def now(self) -> datetime:
        if self._mode == "PRODUCTION_SYSTEM_UTC":
            return datetime.now(timezone.utc)
        if self._mode == "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE" and self._fixed_at is not None:
            return self._fixed_at
        raise ContractError("trusted clock capability is internally invalid")


def require_trusted_clock(clock: TrustedClock | None) -> TrustedClock:
    if clock is None:
        return TrustedClock.production()
    if type(clock) is not TrustedClock:
        raise ContractError("clock must be a repository-issued TrustedClock capability")
    return clock
