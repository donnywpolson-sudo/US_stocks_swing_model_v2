from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .common import (
    _fsync_directory,
    assert_exact_tree,
    atomic_write,
    atomic_write_new,
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    reject_link,
    require_aware_utc,
    require_contained_path,
    require_sha256,
    sha256_bytes,
)
from .errors import ContractError, EvaluationAuthorizationError, IntegrityError
from .eligibility import EligibilityCensus
from .clock import TrustedClock, require_trusted_clock
from .governance import LocalIntegrityRecord
from .locking import ExclusiveFileLock
from .schemas import OutcomeRow, OutcomeStatus, UnderlyingPrediction, assert_underlying_only_payload
from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .corporate_actions import BitemporalActionLedger
from .exchange_calendar import load_xnys_calendar_release
from .outcomes import DailyBar, build_outcome, load_daily_bar_release


LOCAL_ANCHOR_DURABILITY = "LOCAL_TAMPER_EVIDENT_NOT_EXTERNAL_WORM"
OUTCOME_ANCHOR_RECOVERY_SCOPE = "AUTHORIZE_OUTCOME_ANCHOR_RECOVERY"
OUTCOME_ANCHOR_RECOVERY_OPERATION = "ANCHOR_EXACT_COMMITTED_OUTCOME_TAIL"


def _outcome_interval_bars(
    *,
    calendar: Any,
    decision_session: date,
    asset_id: str,
    all_bars: Iterable[DailyBar],
) -> dict[date, DailyBar]:
    """Select only the five-session evidence the outcome builder accepts."""

    horizon = calendar.outcome_sessions(decision_session)
    if horizon is None:
        return {}
    allowed_sessions = set(calendar.interval(*horizon))
    return {
        bar.session: bar
        for bar in all_bars
        if bar.asset_id == asset_id and bar.session in allowed_sessions
    }


class HashChainLedger:
    """Append-only JSONL whose complete history is verified before each append."""

    def __init__(
        self,
        path: Path,
        record_type: str,
        *,
        clock: TrustedClock | None = None,
        unique_key: str | None = None,
        payload_validator: Callable[[Mapping[str, Any]], None] | None = None,
    ):
        if not record_type:
            raise ContractError("record_type is required")
        if unique_key is not None and (
            type(unique_key) is not str
            or not unique_key
            or unique_key != unique_key.strip()
        ):
            raise ContractError("ledger unique key must be canonical nonempty text")
        if payload_validator is not None and not callable(payload_validator):
            raise ContractError("ledger payload validator must be callable")
        self.path = Path(path)
        self.record_type = record_type
        self._clock = require_trusted_clock(clock)
        self._unique_key = unique_key
        self._payload_validator = payload_validator
        self._synthetic_verifier_permit_ids = frozenset(
            ()
            if self._clock.synthetic_permit_id is None
            else (self._clock.synthetic_permit_id,)
        )

    def with_clock(self, clock: TrustedClock) -> "HashChainLedger":
        """Rebind an existing synthetic ledger and retain its verified permit census."""

        self.read_verified()
        rebound = HashChainLedger(
            self.path,
            self.record_type,
            clock=clock,
            unique_key=self._unique_key,
            payload_validator=self._payload_validator,
        )
        if rebound._clock.mode != self._clock.mode:
            raise IntegrityError("ledger clock rebinding cannot change authority mode")
        permit_ids = frozenset(
            set(self._synthetic_verifier_permit_ids)
            | set(rebound._synthetic_verifier_permit_ids)
        )
        self._synthetic_verifier_permit_ids = permit_ids
        rebound._synthetic_verifier_permit_ids = permit_ids
        return rebound

    def authorize_synthetic_history(
        self,
        permit_ids: tuple[str, ...],
        *,
        permit: SyntheticOnlyPermit,
    ) -> None:
        """Authorize an exact synthetic fixture history; never production evidence."""

        require_synthetic_permit(
            permit,
            scope="SYNTHETIC_LEDGER_HISTORY_PERMITS",
        )
        if self._clock.trust_eligible:
            raise ContractError(
                "production ledgers cannot authorize synthetic history permits"
            )
        if (
            type(permit_ids) is not tuple
            or not permit_ids
            or permit_ids != tuple(sorted(set(permit_ids)))
            or self._clock.synthetic_permit_id not in permit_ids
        ):
            raise ContractError(
                "synthetic ledger history permits must be an exact sorted census "
                "including the verifier clock"
            )
        for index, permit_id in enumerate(permit_ids):
            require_sha256(
                permit_id,
                f"ledger.synthetic_history_permit_ids[{index}]",
            )
        self._synthetic_verifier_permit_ids = frozenset(permit_ids)

    @property
    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".writer.lock")

    @property
    def _journal_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".pending.json")

    def read_verified(self) -> list[dict[str, Any]]:
        self._verify_plain_paths()
        if self._journal_path.exists():
            with ExclusiveFileLock(self._lock_path, allowed_root=self.path.parent):
                # Establish that the committed base is sound before treating
                # the journal, rather than the ledger itself, as rejected
                # recovery evidence. An exact already-committed journal tail
                # remains recovery evidence until it is revalidated below.
                history = self._read_verified_raw(
                    validate_payload_contracts=False
                )
                self._validate_committed_payloads(
                    self._committed_base_before_journal(history)
                )
                try:
                    self._recover_locked()
                except IntegrityError:
                    self._quarantine_rejected_journal()
                    raise
        return self._read_verified_raw()

    def _quarantine_rejected_journal(self) -> Path:
        """Preserve invalid recovery bytes once, then unblock public reads."""

        raw = self._journal_path.read_bytes()
        digest = sha256_bytes(raw)
        rejected = self.path.with_name(
            f".{self.path.name}.rejected-journal-{digest}.json"
        )
        reject_link(rejected)
        if rejected.exists():
            if not rejected.is_file() or rejected.stat().st_nlink != 1:
                raise IntegrityError(
                    "rejected ledger journal destination is not an independent plain file"
                )
            if rejected.read_bytes() != raw:
                raise IntegrityError(
                    "rejected ledger journal conflicts with preserved evidence"
                )
        else:
            atomic_write_new(rejected, raw)
        self._journal_path.unlink()
        _fsync_directory(self._journal_path.parent)
        return rejected

    def _read_verified_raw(
        self,
        *,
        validate_payload_contracts: bool = True,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        self._verify_plain_paths()
        records: list[dict[str, Any]] = []
        previous = "0" * 64
        with self.path.open("rb") as handle:
            for sequence, line in enumerate(handle):
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise IntegrityError(f"invalid ledger JSON at sequence {sequence}") from exc
                if set(envelope) != {
                    "sequence",
                    "previous_hash",
                    "record_type",
                    "recorded_at",
                    "time_authority",
                    "synthetic_clock_permit_id",
                    "payload",
                    "record_hash",
                } or not isinstance(envelope.get("payload"), dict):
                    raise IntegrityError(f"ledger envelope schema differs at sequence {sequence}")
                if line != canonical_json_bytes(envelope):
                    raise IntegrityError(f"ledger encoding is noncanonical at sequence {sequence}")
                if envelope.get("sequence") != sequence or envelope.get("previous_hash") != previous:
                    raise IntegrityError(f"ledger chain broken at sequence {sequence}")
                if envelope.get("record_type") != self.record_type:
                    raise IntegrityError(f"wrong ledger record type at sequence {sequence}")
                parse_utc_z(str(envelope.get("recorded_at", "")), "ledger.recorded_at")
                if envelope.get("time_authority") != self._clock.mode:
                    raise IntegrityError("ledger time authority differs from the opened clock mode")
                if self._clock.mode == "PRODUCTION_SYSTEM_UTC":
                    if envelope.get("synthetic_clock_permit_id") is not None:
                        raise IntegrityError("production ledger record carries synthetic time")
                elif (
                    not isinstance(envelope.get("synthetic_clock_permit_id"), str)
                ):
                    raise IntegrityError("synthetic ledger record lacks its fixed-time permit")
                else:
                    try:
                        require_sha256(
                            envelope["synthetic_clock_permit_id"],
                            "ledger.synthetic_clock_permit_id",
                        )
                    except ContractError as exc:
                        raise IntegrityError(str(exc)) from exc
                    if envelope["synthetic_clock_permit_id"] not in (
                        self._synthetic_verifier_permit_ids
                    ):
                        raise IntegrityError(
                            "synthetic ledger record permit is not authorized by this verifier"
                        )
                unsigned = {
                    "sequence": sequence,
                    "previous_hash": previous,
                    "record_type": self.record_type,
                    "recorded_at": envelope.get("recorded_at"),
                    "time_authority": envelope.get("time_authority"),
                    "synthetic_clock_permit_id": envelope.get("synthetic_clock_permit_id"),
                    "payload": envelope.get("payload"),
                }
                expected = sha256_bytes(canonical_json_bytes(unsigned))
                if envelope.get("record_hash") != expected:
                    raise IntegrityError(f"ledger record hash mismatch at sequence {sequence}")
                previous = expected
                records.append(envelope)
        if validate_payload_contracts:
            self._validate_committed_payloads(records)
        return records

    def _committed_base_before_journal(
        self,
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Exclude only an exact journal tail from committed-base preflight."""

        try:
            pending = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return history
        if not isinstance(pending, dict):
            return history
        if "batch_schema_version" in pending:
            envelopes = pending.get("envelopes")
            if not isinstance(envelopes, list) or not envelopes:
                return history
            first = envelopes[0]
            if not isinstance(first, dict):
                return history
            start = first.get("sequence")
            if (
                type(start) is int
                and start >= 0
                and len(history) == start + len(envelopes)
                and history[start:] == envelopes
            ):
                return history[:start]
            return history
        sequence = pending.get("sequence")
        if (
            type(sequence) is int
            and sequence >= 0
            and len(history) == sequence + 1
            and history[-1:] == [pending]
        ):
            return history[:-1]
        return history

    def append(
        self,
        payload: Mapping[str, Any],
        *,
        unique_key: str | None = None,
        payload_validator: Callable[[Mapping[str, Any]], None] | None = None,
        expected_record_count: int | None = None,
        expected_head_hash: str | None = None,
    ) -> dict[str, Any]:
        effective_unique_key, effective_payload_validator = (
            self._resolve_append_contract(
                unique_key=unique_key,
                payload_validator=payload_validator,
            )
        )
        if effective_payload_validator:
            effective_payload_validator(payload)
        self._verify_plain_paths()
        parsed_recorded = require_aware_utc(self._clock.now(), "ledger.clock")
        with ExclusiveFileLock(self._lock_path, allowed_root=self.path.parent):
            self._recover_locked()
            history = self._read_verified_raw()
            actual_head = history[-1]["record_hash"] if history else "0" * 64
            if expected_record_count is not None and len(history) != expected_record_count:
                raise IntegrityError("ledger changed after preflight")
            if expected_head_hash is not None and actual_head != expected_head_hash:
                raise IntegrityError("ledger head changed after preflight")
            if history and parsed_recorded < parse_utc_z(history[-1]["recorded_at"], "previous.recorded_at"):
                raise ContractError("ledger recorded_at must be monotone")
            if effective_unique_key:
                value = payload.get(effective_unique_key)
                if value is None:
                    raise ContractError(f"unique key missing: {effective_unique_key}")
                if any(
                    entry["payload"].get(effective_unique_key) == value
                    for entry in history
                ):
                    raise IntegrityError(
                        f"duplicate append-only key {effective_unique_key}={value}"
                    )
            sequence = len(history)
            previous = history[-1]["record_hash"] if history else "0" * 64
            unsigned = {
                "sequence": sequence,
                "previous_hash": previous,
                "record_type": self.record_type,
                "recorded_at": iso_z(parsed_recorded),
                "time_authority": self._clock.mode,
                "synthetic_clock_permit_id": self._clock.synthetic_permit_id,
                "payload": dict(payload),
            }
            envelope = {**unsigned, "record_hash": sha256_bytes(canonical_json_bytes(unsigned))}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            reject_link(self.path.parent)
            atomic_write(self._journal_path, canonical_json_bytes(envelope))
            existing = self.path.read_bytes() if self.path.exists() else b""
            atomic_write(self.path, existing + canonical_json_bytes(envelope))
            self._read_verified_raw()
            self._journal_path.unlink()
            return envelope

    def append_many(
        self,
        payloads: Iterable[Mapping[str, Any]],
        *,
        unique_key: str | None = None,
        payload_validator: Callable[[Mapping[str, Any]], None] | None = None,
        expected_record_count: int,
        expected_head_hash: str,
    ) -> tuple[dict[str, Any], ...]:
        effective_unique_key, effective_payload_validator = (
            self._resolve_append_contract(
                unique_key=unique_key,
                payload_validator=payload_validator,
            )
        )
        if effective_unique_key is None or effective_payload_validator is None:
            raise ContractError(
                "atomic ledger batches require construction-bound validation "
                "and uniqueness contracts"
            )
        materialized = tuple(dict(payload) for payload in payloads)
        if not materialized:
            raise ContractError("atomic ledger batch cannot be empty")
        for payload in materialized:
            effective_payload_validator(payload)
        values = [
            payload.get(effective_unique_key)
            for payload in materialized
        ]
        if any(value is None for value in values) or len(set(values)) != len(values):
            raise ContractError("atomic ledger batch unique keys are missing or duplicated")
        self._verify_plain_paths()
        recorded = require_aware_utc(self._clock.now(), "ledger.clock")
        with ExclusiveFileLock(self._lock_path, allowed_root=self.path.parent):
            self._recover_locked()
            history = self._read_verified_raw()
            actual_head = history[-1]["record_hash"] if history else "0" * 64
            if len(history) != expected_record_count or actual_head != expected_head_hash:
                raise IntegrityError("ledger changed after atomic batch preflight")
            if any(
                row["payload"].get(effective_unique_key) in set(values)
                for row in history
            ):
                raise IntegrityError("atomic ledger batch duplicates an existing key")
            if history and recorded < parse_utc_z(history[-1]["recorded_at"], "previous.recorded_at"):
                raise ContractError("ledger recorded_at must be monotone")
            envelopes: list[dict[str, Any]] = []
            previous = actual_head
            for offset, payload in enumerate(materialized):
                unsigned = {
                    "sequence": len(history) + offset,
                    "previous_hash": previous,
                    "record_type": self.record_type,
                    "recorded_at": iso_z(recorded),
                    "time_authority": self._clock.mode,
                    "synthetic_clock_permit_id": self._clock.synthetic_permit_id,
                    "payload": payload,
                }
                envelope = {
                    **unsigned,
                    "record_hash": sha256_bytes(canonical_json_bytes(unsigned)),
                }
                envelopes.append(envelope)
                previous = envelope["record_hash"]
            journal = {
                "batch_schema_version": 1,
                "record_type": self.record_type,
                "envelopes": envelopes,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            reject_link(self.path.parent)
            atomic_write(self._journal_path, canonical_json_bytes(journal))
            existing = self.path.read_bytes() if self.path.exists() else b""
            atomic_write(
                self.path,
                existing + b"".join(canonical_json_bytes(item) for item in envelopes),
            )
            self._read_verified_raw()
            self._journal_path.unlink()
            return tuple(envelopes)

    def _recover_locked(self) -> None:
        if not self._journal_path.exists():
            return
        self._verify_plain_paths()
        try:
            pending = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError("ledger recovery journal is invalid") from exc
        if not isinstance(pending, dict):
            raise IntegrityError("ledger recovery journal must be a JSON object")
        history = self._read_verified_raw(validate_payload_contracts=False)
        if isinstance(pending, dict) and "batch_schema_version" in pending:
            if set(pending) != {"batch_schema_version", "record_type", "envelopes"}:
                raise IntegrityError("ledger batch recovery journal fields differ")
            envelopes = pending["envelopes"]
            if (
                pending["batch_schema_version"] != 1
                or pending["record_type"] != self.record_type
                or not isinstance(envelopes, list)
                or not envelopes
            ):
                raise IntegrityError("ledger batch recovery journal is invalid")
            if not isinstance(envelopes[0], dict):
                raise IntegrityError("ledger batch envelope is invalid")
            start = envelopes[0].get("sequence")
            if isinstance(start, bool) or not isinstance(start, int):
                raise IntegrityError("ledger batch recovery sequence is invalid")
            already_committed = (
                len(history) == start + len(envelopes)
                and history[start:] == envelopes
            )
            if not already_committed and len(history) != start:
                raise IntegrityError("ledger batch recovery conflicts with committed history")
            base_history = history[:start] if already_committed else history
            self._validate_committed_payloads(base_history)
            previous = base_history[-1]["record_hash"] if base_history else "0" * 64
            for offset, envelope in enumerate(envelopes):
                self._validate_recovery_envelope(
                    envelope,
                    expected_sequence=start + offset,
                    expected_previous=previous,
                    previous_recorded_at=(
                        base_history[-1]["recorded_at"]
                        if offset == 0 and base_history
                        else envelopes[offset - 1]["recorded_at"]
                        if offset
                        else None
                    ),
                )
                previous = envelope["record_hash"]
            self._validate_recovery_payloads(
                envelopes,
                existing_history=base_history,
            )
            if already_committed:
                self._journal_path.unlink()
                return
            existing = self.path.read_bytes() if self.path.exists() else b""
            atomic_write(
                self.path,
                existing + b"".join(canonical_json_bytes(item) for item in envelopes),
            )
            self._read_verified_raw()
            self._journal_path.unlink()
            return
        sequence = pending.get("sequence")
        already_committed = (
            sequence == len(history) - 1
            and bool(history)
            and history[-1] == pending
        )
        base_history = history[:-1] if already_committed else history
        self._validate_committed_payloads(base_history)
        if already_committed:
            self._validate_recovery_envelope(
                pending,
                expected_sequence=len(base_history),
                expected_previous=(
                    base_history[-1]["record_hash"]
                    if base_history
                    else "0" * 64
                ),
                previous_recorded_at=(
                    base_history[-1]["recorded_at"] if base_history else None
                ),
            )
            self._validate_recovery_payloads(
                (pending,),
                existing_history=base_history,
            )
            self._journal_path.unlink()
            return
        if sequence != len(history):
            raise IntegrityError("ledger recovery journal conflicts with committed history")
        previous = history[-1]["record_hash"] if history else "0" * 64
        self._validate_recovery_envelope(
            pending,
            expected_sequence=len(history),
            expected_previous=previous,
            previous_recorded_at=history[-1]["recorded_at"] if history else None,
        )
        self._validate_recovery_payloads(
            (pending,),
            existing_history=history,
        )
        existing = self.path.read_bytes() if self.path.exists() else b""
        atomic_write(self.path, existing + canonical_json_bytes(pending))
        self._read_verified_raw()
        self._journal_path.unlink()

    def _validate_recovery_envelope(
        self,
        envelope: object,
        *,
        expected_sequence: int,
        expected_previous: str,
        previous_recorded_at: str | None,
    ) -> None:
        fields = {
            "sequence",
            "previous_hash",
            "record_type",
            "recorded_at",
            "time_authority",
            "synthetic_clock_permit_id",
            "payload",
            "record_hash",
        }
        if not isinstance(envelope, dict) or set(envelope) != fields:
            raise IntegrityError("ledger recovery envelope fields differ")
        if not isinstance(envelope["payload"], dict):
            raise IntegrityError("ledger recovery payload must be a JSON object")
        if (
            envelope["sequence"] != expected_sequence
            or isinstance(envelope["sequence"], bool)
            or envelope["previous_hash"] != expected_previous
            or envelope["record_type"] != self.record_type
        ):
            raise IntegrityError("ledger recovery journal chain is invalid")
        try:
            recorded = parse_utc_z(envelope["recorded_at"], "ledger.recorded_at")
        except (ContractError, TypeError) as exc:
            raise IntegrityError("ledger recovery timestamp is invalid") from exc
        if previous_recorded_at is not None and recorded < parse_utc_z(
            previous_recorded_at, "previous.recorded_at"
        ):
            raise IntegrityError("ledger recovery timestamp is nonmonotone")
        if envelope["time_authority"] != self._clock.mode:
            raise IntegrityError("ledger recovery time authority differs")
        if self._clock.mode == "PRODUCTION_SYSTEM_UTC":
            if envelope["synthetic_clock_permit_id"] is not None:
                raise IntegrityError("production recovery carries synthetic time")
        elif not isinstance(envelope["synthetic_clock_permit_id"], str):
            raise IntegrityError("synthetic recovery lacks its fixed-time permit")
        else:
            try:
                require_sha256(
                    envelope["synthetic_clock_permit_id"],
                    "ledger.synthetic_clock_permit_id",
                )
            except ContractError as exc:
                raise IntegrityError(str(exc)) from exc
            if (
                envelope["synthetic_clock_permit_id"]
                not in self._synthetic_verifier_permit_ids
            ):
                raise IntegrityError(
                    "synthetic recovery permit is not authorized by this verifier"
                )
        unsigned = {key: envelope[key] for key in fields if key != "record_hash"}
        if envelope["record_hash"] != sha256_bytes(canonical_json_bytes(unsigned)):
            raise IntegrityError("ledger recovery journal hash is invalid")

    def _resolve_append_contract(
        self,
        *,
        unique_key: str | None,
        payload_validator: Callable[[Mapping[str, Any]], None] | None,
    ) -> tuple[
        str | None,
        Callable[[Mapping[str, Any]], None] | None,
    ]:
        if unique_key is not None and unique_key != self._unique_key:
            raise ContractError(
                "ledger uniqueness contract must be bound at construction"
            )
        if (
            payload_validator is not None
            and payload_validator is not self._payload_validator
        ):
            raise ContractError(
                "ledger payload validator must be bound at construction"
            )
        return self._unique_key, self._payload_validator

    def _validate_recovery_payloads(
        self,
        envelopes: Iterable[Mapping[str, Any]],
        *,
        existing_history: Iterable[Mapping[str, Any]],
    ) -> None:
        self._validate_payload_contracts(
            envelopes,
            existing_history=existing_history,
            context="ledger recovery",
        )

    def _validate_committed_payloads(
        self,
        envelopes: Iterable[Mapping[str, Any]],
    ) -> None:
        self._validate_payload_contracts(
            envelopes,
            existing_history=(),
            context="committed ledger",
        )

    def _validate_payload_contracts(
        self,
        envelopes: Iterable[Mapping[str, Any]],
        *,
        existing_history: Iterable[Mapping[str, Any]],
        context: str,
    ) -> None:
        seen_hashable: set[object] = set()
        seen_unhashable: list[object] = []

        def remember_unique_value(value: object) -> bool:
            try:
                if value in seen_hashable:
                    return False
                seen_hashable.add(value)
            except TypeError:
                if any(existing == value for existing in seen_unhashable):
                    return False
                seen_unhashable.append(value)
            return True

        if self._unique_key is not None:
            for row in existing_history:
                value = row["payload"].get(self._unique_key)
                if value is None:
                    raise IntegrityError(
                        f"{context} unique key is missing: {self._unique_key}"
                    )
                if not remember_unique_value(value):
                    raise IntegrityError(
                        f"{context} duplicates append-only key "
                        f"{self._unique_key}={value}"
                    )
        for envelope in envelopes:
            payload = envelope["payload"]
            if self._payload_validator is not None:
                try:
                    self._payload_validator(payload)
                except ContractError as exc:
                    if context == "committed ledger":
                        raise
                    raise IntegrityError(
                        f"{context} payload fails its record-type validator"
                    ) from exc
                except (
                    IntegrityError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise IntegrityError(
                        f"{context} payload fails its record-type validator"
                    ) from exc
            if self._unique_key is None:
                continue
            value = payload.get(self._unique_key)
            if value is None:
                raise IntegrityError(
                    f"{context} unique key is missing: {self._unique_key}"
                )
            if not remember_unique_value(value):
                raise IntegrityError(
                    f"{context} duplicates append-only key "
                    f"{self._unique_key}={value}"
                )

    def _verify_plain_paths(self) -> None:
        for candidate in (self.path, self._journal_path):
            if candidate.exists():
                reject_link(candidate)
                if not candidate.is_file() or candidate.stat().st_nlink != 1:
                    raise IntegrityError(f"ledger path is not an independent plain file: {candidate}")


@dataclass(frozen=True)
class LedgerAnchorReceipt:
    schema_version: int
    ledger_identity: str
    record_type: str
    record_count: int
    head_hash: str
    ledger_sha256: str
    previous_anchor_id: str | None
    durability: str
    anchored_at: str
    time_authority: str
    synthetic_clock_permit_id: str | None
    recovery_record_id: str | None
    anchor_id: str

    def unsigned_dict(self) -> dict[str, object]:
        value = {
            "schema_version": self.schema_version,
            "ledger_identity": self.ledger_identity,
            "record_type": self.record_type,
            "record_count": self.record_count,
            "head_hash": self.head_hash,
            "ledger_sha256": self.ledger_sha256,
            "previous_anchor_id": self.previous_anchor_id,
            "durability": self.durability,
            "anchored_at": self.anchored_at,
            "time_authority": self.time_authority,
            "synthetic_clock_permit_id": self.synthetic_clock_permit_id,
        }
        if self.schema_version == 2:
            value["recovery_record_id"] = self.recovery_record_id
        return value


class LedgerAnchorStore:
    """Local content-addressed receipts stored outside the mutable ledger tree.

    These receipts detect local mutation when retained, but they are not an
    independent timestamp authority and are not external/WORM evidence.
    """

    def __init__(
        self,
        root: Path,
        ledger: HashChainLedger,
        *,
        clock: TrustedClock | None = None,
    ):
        self.root = Path(root)
        self.ledger = ledger
        self._clock = require_trusted_clock(clock)
        ledger_parent = ledger.path.absolute().parent
        anchor_root = self.root.absolute()
        if anchor_root == ledger_parent or anchor_root in ledger_parent.parents or ledger_parent in anchor_root.parents:
            raise ContractError("ledger anchors must use a separate, non-nested tree")

    @property
    def ledger_identity(self) -> str:
        return sha256_bytes(canonical_json_bytes({"path": str(self.ledger.path.absolute())}))

    def observed_at(self) -> datetime:
        return require_aware_utc(self._clock.now(), "anchor.clock")

    def _recovery_contract(
        self,
        history: list[dict[str, Any]],
        *,
        previous_anchor_id: str | None,
    ) -> tuple[str, dict[str, str]]:
        if not history:
            raise ContractError("cannot recover an empty ledger")
        ledger_bytes = b"".join(canonical_json_bytes(row) for row in history)
        tail = history[-1]
        bindings = {
            "operation": OUTCOME_ANCHOR_RECOVERY_OPERATION,
            "ledger_identity": self.ledger_identity,
            "record_type": self.ledger.record_type,
            "record_count": str(len(history)),
            "ledger_sha256": sha256_bytes(ledger_bytes),
            "head_hash": str(tail["record_hash"]),
            "previous_anchor_id": previous_anchor_id or "NONE",
            "tail_payload_sha256": sha256_bytes(
                canonical_json_bytes(tail["payload"])
            ),
            "tail_recorded_at": str(tail["recorded_at"]),
        }
        unsigned = {
            "schema_version": 1,
            "scope": OUTCOME_ANCHOR_RECOVERY_SCOPE,
            "subject_id": self.ledger_identity,
            "bindings": bindings,
        }
        return sha256_bytes(canonical_json_bytes(unsigned)), bindings

    def _assert_no_current_or_partial_anchor(
        self,
        history: list[dict[str, Any]],
    ) -> None:
        if not self.root.exists():
            return
        reject_link(self.root)
        if not self.root.is_dir():
            raise IntegrityError("ledger anchor root is not a directory")
        expected_ledger_sha256 = sha256_bytes(
            b"".join(canonical_json_bytes(row) for row in history)
        )
        for candidate in self.root.iterdir():
            reject_link(candidate)
            if candidate.name.startswith(".pending-"):
                raise IntegrityError(
                    "partial ledger anchor evidence requires explicit disposition"
                )
            try:
                require_sha256(candidate.name, "ledger anchor directory")
            except ContractError as exc:
                raise IntegrityError("unexpected ledger anchor root entry") from exc
            receipt = self.load(candidate)
            if (
                receipt.ledger_identity == self.ledger_identity
                and receipt.record_type == self.ledger.record_type
                and receipt.record_count == len(history)
                and receipt.head_hash == history[-1]["record_hash"]
                and receipt.ledger_sha256 == expected_ledger_sha256
            ):
                raise IntegrityError("committed ledger tail is already anchored")

    def recovery_review_contract(
        self,
        history: list[dict[str, Any]],
        *,
        previous_anchor: Path | None,
        prior_record_count: int,
    ) -> dict[str, object]:
        if prior_record_count != len(history) - 1:
            raise IntegrityError(
                "anchor recovery requires exactly one committed unanchored record"
            )
        prior_history = history[:prior_record_count]
        previous_anchor_id: str | None = None
        if prior_history:
            if previous_anchor is None:
                raise IntegrityError(
                    "anchor recovery requires the exact retained prior receipt"
                )
            prior = self.verify(previous_anchor, prior_history)
            previous_anchor_id = prior.anchor_id
        elif previous_anchor is not None:
            raise IntegrityError("first anchor recovery cannot claim a predecessor")
        self._assert_no_current_or_partial_anchor(history)
        plan_id, bindings = self._recovery_contract(
            history,
            previous_anchor_id=previous_anchor_id,
        )
        return {
            "schema_version": 1,
            "mode": "OUTCOME_ANCHOR_RECOVERY_PLAN_ONLY_NO_WRITES",
            "scope": OUTCOME_ANCHOR_RECOVERY_SCOPE,
            "recovery_plan_id": plan_id,
            "subject_id": plan_id,
            "bindings": bindings,
            "execution_authorized": False,
            "outcome_access_authorized": False,
            "research_or_activation_authorized": False,
        }

    def create(
        self,
        history: list[dict[str, Any]],
        *,
        previous_anchor: Path | None,
        prior_record_count: int | None = None,
    ) -> Path:
        return self._create(
            history,
            previous_anchor=previous_anchor,
            prior_record_count=prior_record_count,
            recovery_authorization=None,
        )

    def create_recovered(
        self,
        history: list[dict[str, Any]],
        *,
        previous_anchor: Path | None,
        prior_record_count: int,
        recovery_authorization: LocalIntegrityRecord,
    ) -> Path:
        if type(recovery_authorization) is not LocalIntegrityRecord:
            raise ContractError(
                "anchor recovery requires an exact local integrity record"
            )
        recovery_lock = (
            self.root.parent
            / ".locks"
            / f"{self.root.name}.outcome-anchor-recovery.lock"
        )
        with ExclusiveFileLock(recovery_lock, allowed_root=self.root.parent):
            plan = self.recovery_review_contract(
                history,
                previous_anchor=previous_anchor,
                prior_record_count=prior_record_count,
            )
            recovery_authorization.validate(
                expected_scope=OUTCOME_ANCHOR_RECOVERY_SCOPE,
                expected_subject_id=str(plan["subject_id"]),
                required_bindings=plan["bindings"],
                clock=self._clock,
            )
            return self._create(
                history,
                previous_anchor=previous_anchor,
                prior_record_count=prior_record_count,
                recovery_authorization=recovery_authorization,
            )

    def _create(
        self,
        history: list[dict[str, Any]],
        *,
        previous_anchor: Path | None,
        prior_record_count: int | None,
        recovery_authorization: LocalIntegrityRecord | None,
    ) -> Path:
        anchored_at = self.observed_at()
        if not history:
            raise ContractError("cannot anchor an empty ledger")
        previous_id: str | None = None
        if prior_record_count is not None:
            if (
                isinstance(prior_record_count, bool)
                or not isinstance(prior_record_count, int)
                or prior_record_count < 0
                or prior_record_count >= len(history)
            ):
                raise IntegrityError("anchor prior record count is invalid")
            prior_history = history[:prior_record_count]
        else:
            prior_history = history[:-1]
        if not prior_history:
            if previous_anchor is not None:
                raise IntegrityError("first ledger anchor cannot claim a predecessor")
        else:
            if previous_anchor is None:
                raise IntegrityError("non-first ledger anchor requires the exact prior receipt")
            prior = self.load(previous_anchor)
            self._verify_against(prior, prior_history)
            previous_id = prior.anchor_id
        ledger_bytes = b"".join(canonical_json_bytes(row) for row in history)
        unsigned = {
            "schema_version": 2 if recovery_authorization is not None else 1,
            "ledger_identity": self.ledger_identity,
            "record_type": self.ledger.record_type,
            "record_count": len(history),
            "head_hash": history[-1]["record_hash"],
            "ledger_sha256": sha256_bytes(ledger_bytes),
            "previous_anchor_id": previous_id,
            "durability": LOCAL_ANCHOR_DURABILITY,
            "anchored_at": iso_z(require_aware_utc(anchored_at, "anchor.anchored_at")),
            "time_authority": self._clock.mode,
            "synthetic_clock_permit_id": self._clock.synthetic_permit_id,
        }
        if recovery_authorization is not None:
            unsigned["recovery_record_id"] = recovery_authorization.record_id
        if parse_utc_z(unsigned["anchored_at"], "anchor.anchored_at") < parse_utc_z(
            history[-1]["recorded_at"], "ledger.recorded_at"
        ):
            raise IntegrityError("local anchor receipt cannot predate the ledger head")
        receipt_fields = dict(unsigned)
        receipt_fields.setdefault("recovery_record_id", None)
        receipt = LedgerAnchorReceipt(
            **receipt_fields,
            anchor_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        final = self.root / receipt.anchor_id
        self.root.mkdir(parents=True, exist_ok=True)
        reject_link(self.root)
        if final.exists():
            if self.load(final) != receipt:
                raise IntegrityError("content-addressed ledger anchor collision")
            return final
        pending = self.root / f".pending-{receipt.anchor_id[:12]}-{uuid.uuid4().hex[:8]}"
        pending.mkdir()
        atomic_write(pending / "receipt.json", canonical_json_bytes({**receipt.unsigned_dict(), "anchor_id": receipt.anchor_id}))
        if recovery_authorization is not None:
            atomic_write(
                pending / "recovery.json",
                canonical_json_bytes(recovery_authorization.as_dict()),
            )
        self.load(pending, allow_pending=True)
        os.replace(pending, final)
        self.load(final)
        return final

    def load(self, directory: Path, *, allow_pending: bool = False) -> LedgerAnchorReceipt:
        try:
            path = require_contained_path(
                Path(directory).absolute(),
                self.root.absolute(),
            )
        except ContractError as exc:
            raise IntegrityError(
                "ledger anchor path differs from its approved root"
            ) from exc
        reject_link(path)
        try:
            receipt_path = path / "receipt.json"
            reject_link(receipt_path)
            if receipt_path.stat().st_nlink != 1:
                raise ContractError("anchor receipt is hardlinked")
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ContractError) as exc:
            raise IntegrityError("ledger anchor tree/receipt is invalid") from exc
        if type(payload) is not dict:
            raise IntegrityError("ledger anchor fields differ from the exact contract")
        v1_fields = set(LedgerAnchorReceipt.__dataclass_fields__) - {
            "recovery_record_id"
        }
        v2_fields = set(LedgerAnchorReceipt.__dataclass_fields__)
        if payload.get("schema_version") == 1 and set(payload) == v1_fields:
            expected_files = {"receipt.json"}
            payload["recovery_record_id"] = None
        elif payload.get("schema_version") == 2 and set(payload) == v2_fields:
            expected_files = {"receipt.json", "recovery.json"}
        else:
            raise IntegrityError("ledger anchor fields differ from the exact contract")
        try:
            assert_exact_tree(path, expected_files, set())
        except ContractError as exc:
            raise IntegrityError("ledger anchor tree/receipt is invalid") from exc
        receipt = LedgerAnchorReceipt(**payload)
        parse_utc_z(receipt.anchored_at, "anchor.anchored_at")
        if (
            type(receipt.schema_version) is not int
            or receipt.schema_version not in {1, 2}
            or receipt.record_count < 1
            or receipt.durability != LOCAL_ANCHOR_DURABILITY
            or receipt.time_authority != self._clock.mode
        ):
            raise IntegrityError("ledger anchor schema/count is invalid")
        if receipt.schema_version == 1:
            if receipt.recovery_record_id is not None:
                raise IntegrityError("ordinary ledger anchor carries recovery evidence")
        else:
            try:
                require_sha256(
                    receipt.recovery_record_id or "",
                    "anchor.recovery_record_id",
                )
                recovery = LocalIntegrityRecord.from_dict(
                    json.loads((path / "recovery.json").read_text(encoding="utf-8"))
                )
            except (
                OSError,
                json.JSONDecodeError,
                ContractError,
                EvaluationAuthorizationError,
            ) as exc:
                raise IntegrityError("ledger anchor recovery evidence is invalid") from exc
            if recovery.record_id != receipt.recovery_record_id:
                raise IntegrityError(
                    "ledger anchor recovery record differs from its receipt"
                )
        if receipt.time_authority == "PRODUCTION_SYSTEM_UTC":
            if receipt.synthetic_clock_permit_id is not None:
                raise IntegrityError("production anchor carries synthetic time")
        elif (
            not isinstance(receipt.synthetic_clock_permit_id, str)
        ):
            raise IntegrityError("synthetic anchor lacks its fixed-time permit")
        else:
            try:
                require_sha256(
                    receipt.synthetic_clock_permit_id,
                    "anchor.synthetic_clock_permit_id",
                )
            except ContractError as exc:
                raise IntegrityError(str(exc)) from exc
            if receipt.synthetic_clock_permit_id not in (
                self.ledger._synthetic_verifier_permit_ids
            ):
                raise IntegrityError(
                    "synthetic anchor permit is not authorized by this verifier"
                )
        if receipt.anchor_id != sha256_bytes(canonical_json_bytes(receipt.unsigned_dict())):
            raise IntegrityError("ledger anchor ID differs from its receipt")
        if not allow_pending and path.name != receipt.anchor_id:
            raise IntegrityError("ledger anchor path is not content addressed")
        if allow_pending and not path.name.startswith(".pending-"):
            raise IntegrityError("temporary ledger anchor path is invalid")
        return receipt

    def verify(self, directory: Path, history: list[dict[str, Any]]) -> LedgerAnchorReceipt:
        receipt = self.load(directory)
        self._verify_against(receipt, history)
        if receipt.schema_version == 2:
            recovery = LocalIntegrityRecord.from_dict(
                json.loads((Path(directory) / "recovery.json").read_text(encoding="utf-8"))
            )
            plan_id, bindings = self._recovery_contract(
                history,
                previous_anchor_id=receipt.previous_anchor_id,
            )
            if plan_id != recovery.subject_id:
                raise IntegrityError("ledger anchor recovery plan identity differs")
            try:
                recovery.validate(
                    expected_scope=OUTCOME_ANCHOR_RECOVERY_SCOPE,
                    expected_subject_id=plan_id,
                    required_bindings=bindings,
                    clock=self._clock,
                )
            except EvaluationAuthorizationError as exc:
                raise IntegrityError(
                    "ledger anchor recovery authorization differs"
                ) from exc
            prior_history = history[:-1]
            if prior_history:
                if receipt.previous_anchor_id is None:
                    raise IntegrityError(
                        "recovered ledger anchor lacks its prior receipt"
                    )
                prior_path = self.root / receipt.previous_anchor_id
                self.verify(prior_path, prior_history)
            elif receipt.previous_anchor_id is not None:
                raise IntegrityError(
                    "first recovered ledger anchor claims a predecessor"
                )
        return receipt

    def _verify_against(self, receipt: LedgerAnchorReceipt, history: list[dict[str, Any]]) -> None:
        ledger_bytes = b"".join(canonical_json_bytes(row) for row in history)
        if (
            receipt.ledger_identity != self.ledger_identity
            or receipt.record_type != self.ledger.record_type
            or receipt.record_count != len(history)
            or not history
            or receipt.head_hash != history[-1]["record_hash"]
            or receipt.ledger_sha256 != sha256_bytes(ledger_bytes)
        ):
            raise IntegrityError("ledger differs from the retained local tamper-evident head anchor")


class PredictionLedger:
    def __init__(
        self,
        path: Path,
        anchor_root: Path,
        *,
        clock: TrustedClock | None = None,
    ):
        trusted_clock = require_trusted_clock(clock)
        self._clock = trusted_clock
        self._ledger = HashChainLedger(
            path,
            "underlying_prediction_v1",
            clock=trusted_clock,
            unique_key="prediction_id",
            payload_validator=_validate_prediction_payload,
        )
        self._anchors = LedgerAnchorStore(anchor_root, self._ledger, clock=trusted_clock)

    def append(
        self,
        prediction: UnderlyingPrediction,
        *,
        previous_anchor: Path | None = None,
    ) -> dict[str, Any]:
        raise ContractError(
            "one-row prediction append is prohibited; commit an exact eligibility census atomically"
        )

    def append_synthetic(
        self,
        prediction: UnderlyingPrediction,
        *,
        synthetic_permit: SyntheticOnlyPermit,
        previous_anchor: Path | None = None,
    ) -> dict[str, Any]:
        require_synthetic_permit(
            synthetic_permit,
            scope="SYNTHETIC_SINGLE_PREDICTION_LEDGER_APPEND",
        )
        if self._clock.trust_eligible:
            raise ContractError("production prediction ledgers prohibit single-row append")
        prediction.validate()
        if prediction.time_authority != self._clock.mode:
            raise IntegrityError("prediction and ledger time authorities differ")
        before = self._ledger.read_verified()
        if before:
            if previous_anchor is None:
                raise IntegrityError("prediction append requires the retained prior local anchor receipt")
            self._anchors.verify(previous_anchor, before)
        elif previous_anchor is not None:
            raise IntegrityError("empty prediction ledger cannot use a prior anchor")
        anchored_at = self._anchors.observed_at()
        if anchored_at < prediction.recorded_at:
            raise IntegrityError("local anchor clock predates the prediction record")
        if anchored_at > prediction.prediction_deadline_at or anchored_at >= prediction.information_barrier_at:
            raise IntegrityError("prediction cannot be committed after its anchor deadline/barrier")
        expected_head = before[-1]["record_hash"] if before else "0" * 64
        envelope = self._ledger.append(
            prediction.as_dict(),
            unique_key="prediction_id",
            payload_validator=_validate_prediction_payload,
            expected_record_count=len(before),
            expected_head_hash=expected_head,
        )
        history = self._ledger.read_verified()
        anchor = self._anchors.create(
            history,
            previous_anchor=previous_anchor,
            prior_record_count=len(before),
        )
        return {"envelope": envelope, "anchor_path": str(anchor)}

    def _append_census_from_engine(
        self,
        predictions: Iterable[UnderlyingPrediction],
        *,
        census: EligibilityCensus,
        bundle_id: str,
        feature_release_id: str,
        previous_anchor: Path | None = None,
    ) -> Mapping[str, object]:
        census.validate()
        if (
            census.evidence_state == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            or census.synthetic_permit_id
        ):
            raise ContractError(
                "production prediction commit rejects synthetic-only eligibility census"
            )
        require_sha256(bundle_id, "prediction_commit.bundle_id")
        require_sha256(feature_release_id, "prediction_commit.feature_release_id")
        rows = tuple(predictions)
        if tuple(sorted(row.asset_id for row in rows)) != census.expected_asset_ids:
            raise IntegrityError("prediction batch does not exactly cover the eligibility census")
        if len({row.asset_id for row in rows}) != len(rows):
            raise IntegrityError("prediction batch contains duplicate assets")
        for row in rows:
            row.validate()
            if (
                row.time_authority != self._clock.mode
                or row.eligibility_census_id != census.census_id
                or row.bundle_id != bundle_id
                or row.feature_release_id != feature_release_id
                or row.decision_session != census.decision_session
            ):
                raise IntegrityError("prediction batch row differs from its census/bundle/release")
        before = self._ledger.read_verified()
        if before:
            if previous_anchor is None:
                raise IntegrityError("prediction census append requires the retained prior anchor")
            self._anchors.verify(previous_anchor, before)
        elif previous_anchor is not None:
            raise IntegrityError("empty prediction ledger cannot use a prior anchor")
        anchored_at = self._anchors.observed_at()
        if any(
            anchored_at < row.recorded_at
            or anchored_at > row.prediction_deadline_at
            or anchored_at >= row.information_barrier_at
            for row in rows
        ):
            raise IntegrityError("prediction census cannot commit outside its common safe window")
        expected_head = before[-1]["record_hash"] if before else "0" * 64
        payloads = tuple(row.as_dict() for row in sorted(rows, key=lambda item: item.asset_id))
        self._ledger.append_many(
            payloads,
            unique_key="prediction_id",
            payload_validator=_validate_prediction_payload,
            expected_record_count=len(before),
            expected_head_hash=expected_head,
        )
        history = self._ledger.read_verified()
        anchor = self._anchors.create(
            history,
            previous_anchor=previous_anchor,
            prior_record_count=len(before),
        )
        commit_unsigned = {
            "schema_version": 1,
            "census_id": census.census_id,
            "bundle_id": bundle_id,
            "feature_release_id": feature_release_id,
            "prediction_ids": [payload["prediction_id"] for payload in payloads],
            "prediction_count": len(payloads),
            "anchor_id": anchor.name,
        }
        return {
            **commit_unsigned,
            "commit_id": sha256_bytes(canonical_json_bytes(commit_unsigned)),
            "anchor_path": str(anchor),
        }

    def verify(self, anchor_receipt: Path) -> list[dict[str, Any]]:
        history = self._ledger.read_verified()
        self._anchors.verify(anchor_receipt, history)
        for row in history:
            _validate_prediction_payload(row["payload"])
        return history

    def verify_expected_census(
        self,
        anchor_receipt: Path,
        census: EligibilityCensus,
    ) -> tuple[Mapping[str, Any], ...]:
        census.validate()
        payloads = tuple(
            row["payload"]
            for row in self.verify(anchor_receipt)
            if row["payload"].get("eligibility_census_id") == census.census_id
        )
        asset_ids = tuple(sorted(str(payload["asset_id"]) for payload in payloads))
        if asset_ids != census.expected_asset_ids:
            raise IntegrityError("prediction ledger does not exactly cover the eligibility census")
        return tuple(sorted(payloads, key=lambda payload: str(payload["asset_id"])))


def _validate_prediction_payload(payload: Mapping[str, Any]) -> None:
    assert_underlying_only_payload(payload)
    UnderlyingPrediction.from_dict(payload)


class OutcomeLedger:
    def __init__(
        self,
        path: Path,
        prediction_ledger: PredictionLedger,
        *,
        anchor_root: Path,
        clock: TrustedClock | None = None,
    ):
        self._clock = require_trusted_clock(clock)
        self._ledger = HashChainLedger(
            path,
            "underlying_outcome_v1",
            clock=self._clock,
            unique_key="revision_id",
            payload_validator=_validate_outcome_payload,
        )
        self._anchors = LedgerAnchorStore(
            anchor_root,
            self._ledger,
            clock=self._clock,
        )
        self._predictions = prediction_ledger

    def append(
        self,
        outcome: OutcomeRow,
        *,
        prediction_anchor: Path,
    ) -> dict[str, Any]:
        raise ContractError(
            "caller-constructed outcomes are prohibited; mature from accepted release payloads"
        )

    def append_synthetic(
        self,
        outcome: OutcomeRow,
        *,
        prediction_anchor: Path,
        synthetic_permit: SyntheticOnlyPermit,
        previous_anchor: Path | None = None,
    ) -> dict[str, Any]:
        require_synthetic_permit(
            synthetic_permit,
            scope="SYNTHETIC_OUTCOME_LEDGER_APPEND",
        )
        if self._clock.trust_eligible:
            raise ContractError("production outcome ledgers reject caller-constructed rows")
        return self._append_verified(
            outcome,
            prediction_anchor=prediction_anchor,
            previous_anchor=previous_anchor,
        )

    def mature_from_releases(
        self,
        prediction_id: str,
        *,
        prediction_anchor: Path,
        accepted_release_root: Path,
        calendar_release_directory: Path,
        bar_release_directory: Path,
        action_release_directory: Path,
        previous_anchor: Path | None = None,
    ) -> dict[str, Any]:
        predictions = {
            row["payload"]["prediction_id"]: UnderlyingPrediction.from_dict(row["payload"])
            for row in self._predictions.verify(prediction_anchor)
        }
        prediction = predictions.get(prediction_id)
        if prediction is None:
            raise ContractError("outcome maturation requires a committed prediction")
        calendar = load_xnys_calendar_release(
            calendar_release_directory,
            accepted_release_root=accepted_release_root,
        ).calendar
        bar_manifest, all_bars = load_daily_bar_release(
            bar_release_directory,
            accepted_release_root=accepted_release_root,
        )
        actions = BitemporalActionLedger(
            verified_release_directory=action_release_directory,
            accepted_release_root=accepted_release_root,
            clock=self._clock,
        )
        if actions.trust_eligible is not True:
            raise ContractError(
                "outcome maturation requires a trust-eligible corporate-action ledger"
            )
        if (
            calendar.release_id != prediction.calendar_release_id
            or actions.release_id != prediction.action_release_id
            or bar_manifest.source_epoch != prediction.source_epoch
        ):
            raise ContractError("outcome release identities differ from the committed prediction")
        bars = _outcome_interval_bars(
            calendar=calendar,
            decision_session=prediction.decision_session,
            asset_id=prediction.asset_id,
            all_bars=all_bars,
        )
        prior_history = self._verify_prior_history(previous_anchor)
        prior_rows = [
            OutcomeRow.from_dict(row["payload"])
            for row in prior_history
            if row["payload"].get("prediction_id") == prediction.prediction_id
        ]
        prior = prior_rows[-1] if prior_rows else None
        outcome = build_outcome(
            prediction_id=prediction.prediction_id,
            eligibility_census_id=prediction.eligibility_census_id,
            asset_id=prediction.asset_id,
            decision_session=prediction.decision_session,
            calendar=calendar,
            bars=bars,
            bar_release_id=bar_manifest.release_id,
            actions=actions,
            action_view_as_of=require_aware_utc(self._clock.now(), "outcome.clock"),
            source_epoch=bar_manifest.source_epoch,
            revision_number=1 if prior is None else prior.revision_number + 1,
            prior_revision_id=None if prior is None else prior.revision_id,
        )
        receipt = self._append_verified(
            outcome,
            prediction_anchor=prediction_anchor,
            previous_anchor=previous_anchor,
        )
        return {**receipt, "outcome_revision_id": outcome.revision_id}

    def _verify_prior_history(
        self,
        previous_anchor: Path | None,
    ) -> list[dict[str, Any]]:
        history = self._ledger.read_verified()
        if history:
            if previous_anchor is None:
                raise IntegrityError(
                    "outcome append requires the retained prior local anchor"
                )
            self._anchors.verify(previous_anchor, history)
        elif previous_anchor is not None:
            raise IntegrityError("empty outcome ledger cannot use a prior anchor")
        return history

    def _append_verified(
        self,
        outcome: OutcomeRow,
        *,
        prediction_anchor: Path,
        previous_anchor: Path | None,
    ) -> dict[str, Any]:
        outcome.validate()
        recorded = require_aware_utc(self._clock.now(), "outcome.clock")
        predictions = {
            row["payload"]["prediction_id"]: row["payload"]
            for row in self._predictions.verify(prediction_anchor)
        }
        prediction = predictions.get(outcome.prediction_id)
        if prediction is None:
            raise ContractError("outcome has no prior append-only prediction")
        if recorded <= parse_utc_z(prediction["recorded_at"], "prediction.recorded_at"):
            raise ContractError("outcome must be recorded after its prediction")
        if outcome.action_view_as_of > recorded:
            raise ContractError("outcome action view cannot be later than its ledger record")
        expected_identity = {
            "asset_id": prediction["asset_id"],
            "eligibility_census_id": prediction["eligibility_census_id"],
            "decision_session": prediction["decision_session"],
            "calendar_release_id": prediction["calendar_release_id"],
            "action_release_id": prediction["action_release_id"],
            "source_epoch": prediction["source_epoch"],
        }
        actual_identity = {
            "asset_id": outcome.asset_id,
            "eligibility_census_id": outcome.eligibility_census_id,
            "decision_session": outcome.decision_session.isoformat(),
            "calendar_release_id": outcome.calendar_release_id,
            "action_release_id": outcome.action_release_id,
            "source_epoch": outcome.source_epoch,
        }
        if actual_identity != expected_identity:
            raise ContractError("outcome identity/calendar/source epoch differs from its prediction")

        before = self._verify_prior_history(previous_anchor)
        prior_rows = [
            OutcomeRow.from_dict(row["payload"])
            for row in before
            if row["payload"].get("prediction_id") == outcome.prediction_id
        ]
        if not prior_rows:
            if outcome.revision_number != 1 or outcome.prior_revision_id is not None:
                raise ContractError("first outcome append must be revision one")
        else:
            prior = prior_rows[-1]
            if (
                outcome.revision_number != prior.revision_number + 1
                or outcome.prior_revision_id != prior.revision_id
            ):
                raise ContractError("outcome revision does not extend the latest revision")
            if outcome.action_view_as_of < prior.action_view_as_of:
                raise ContractError("outcome action view must be monotone across revisions")
            if prior.status is not OutcomeStatus.PENDING and outcome.status is OutcomeStatus.PENDING:
                raise ContractError("terminal outcome cannot be revised back to pending")
            immutable_identity = (
                outcome.asset_id,
                outcome.eligibility_census_id,
                outcome.decision_session,
                outcome.calendar_release_id,
                outcome.action_release_id,
                outcome.source_epoch,
            )
            prior_identity = (
                prior.asset_id,
                prior.eligibility_census_id,
                prior.decision_session,
                prior.calendar_release_id,
                prior.action_release_id,
                prior.source_epoch,
            )
            if immutable_identity != prior_identity:
                raise ContractError("outcome lifecycle identity cannot change")
        expected_head = before[-1]["record_hash"] if before else "0" * 64
        envelope = self._ledger.append(
            outcome.as_dict(),
            unique_key="revision_id",
            payload_validator=_validate_outcome_payload,
            expected_record_count=len(before),
            expected_head_hash=expected_head,
        )
        history = self._ledger.read_verified()
        anchor = self._anchors.create(
            history,
            previous_anchor=previous_anchor,
            prior_record_count=len(before),
        )
        return {"envelope": envelope, "anchor_path": str(anchor)}

    def build_unanchored_tail_recovery_plan(
        self,
        intended_outcome: OutcomeRow,
        *,
        prediction_anchor: Path,
        previous_anchor: Path | None = None,
    ) -> dict[str, object]:
        """Bind explicit review of one committed outcome whose anchor is absent."""

        intended_outcome.validate()
        history = self._ledger.read_verified()
        if not history:
            raise IntegrityError("outcome anchor recovery requires a committed tail")
        tail = OutcomeRow.from_dict(history[-1]["payload"])
        if tail != intended_outcome:
            raise IntegrityError(
                "outcome anchor recovery intended record differs from the committed tail"
            )
        predictions = {
            row["payload"]["prediction_id"]: row["payload"]
            for row in self._predictions.verify(prediction_anchor)
        }
        prediction = predictions.get(tail.prediction_id)
        if prediction is None:
            raise IntegrityError(
                "outcome anchor recovery tail lacks its anchored prediction"
            )
        expected_identity = {
            "asset_id": prediction["asset_id"],
            "eligibility_census_id": prediction["eligibility_census_id"],
            "decision_session": prediction["decision_session"],
            "calendar_release_id": prediction["calendar_release_id"],
            "action_release_id": prediction["action_release_id"],
            "source_epoch": prediction["source_epoch"],
        }
        actual_identity = {
            "asset_id": tail.asset_id,
            "eligibility_census_id": tail.eligibility_census_id,
            "decision_session": tail.decision_session.isoformat(),
            "calendar_release_id": tail.calendar_release_id,
            "action_release_id": tail.action_release_id,
            "source_epoch": tail.source_epoch,
        }
        if actual_identity != expected_identity:
            raise IntegrityError(
                "outcome anchor recovery tail differs from its prediction identity"
            )
        return self._anchors.recovery_review_contract(
            history,
            previous_anchor=previous_anchor,
            prior_record_count=len(history) - 1,
        )

    def recover_unanchored_tail(
        self,
        intended_outcome: OutcomeRow,
        *,
        prediction_anchor: Path,
        recovery_authorization: LocalIntegrityRecord,
        previous_anchor: Path | None = None,
    ) -> dict[str, object]:
        """Create only the missing anchor after exact owner-reviewed recovery."""

        plan = self.build_unanchored_tail_recovery_plan(
            intended_outcome,
            prediction_anchor=prediction_anchor,
            previous_anchor=previous_anchor,
        )
        history = self._ledger.read_verified()
        anchor = self._anchors.create_recovered(
            history,
            previous_anchor=previous_anchor,
            prior_record_count=len(history) - 1,
            recovery_authorization=recovery_authorization,
        )
        self.verify(anchor)
        return {
            "schema_version": 1,
            "mode": "RECOVERED_EXACT_COMMITTED_OUTCOME_TAIL",
            "recovery_plan_id": plan["recovery_plan_id"],
            "recovery_record_id": recovery_authorization.record_id,
            "outcome_revision_id": intended_outcome.revision_id,
            "anchor_path": str(anchor),
            "outcome_access_authorized": False,
            "research_or_activation_authorized": False,
        }

    def verify(self, anchor_receipt: Path) -> list[dict[str, Any]]:
        history = self._ledger.read_verified()
        self._anchors.verify(anchor_receipt, history)
        for row in history:
            _validate_outcome_payload(row["payload"])
        return history

    def verify_expected_census(
        self,
        census: EligibilityCensus,
        *,
        prediction_anchor: Path,
        outcome_anchor: Path,
    ) -> tuple[OutcomeRow, ...]:
        predictions = self._predictions.verify_expected_census(prediction_anchor, census)
        expected_prediction_ids = {str(payload["prediction_id"]) for payload in predictions}
        latest: dict[str, OutcomeRow] = {}
        for envelope in self.verify(outcome_anchor):
            row = OutcomeRow.from_dict(envelope["payload"])
            if row.eligibility_census_id != census.census_id:
                continue
            current = latest.get(row.prediction_id)
            if current is None or row.revision_number > current.revision_number:
                latest[row.prediction_id] = row
        if set(latest) != expected_prediction_ids:
            raise IntegrityError("outcome ledger does not exactly cover the eligibility census")
        return tuple(latest[item] for item in sorted(latest))


def _validate_outcome_payload(payload: Mapping[str, Any]) -> None:
    try:
        OutcomeRow.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise ContractError("outcome ledger payload is malformed") from exc
