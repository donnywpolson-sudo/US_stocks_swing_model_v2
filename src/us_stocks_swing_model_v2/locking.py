from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common import canonical_json_bytes
from .errors import LockHeldError


class ExclusiveFileLock:
    """A fail-closed, non-stealing one-writer lock.

    Stale locks are never guessed from age. Recovery requires a deliberate
    operator action because automatically stealing a slow writer's lock can
    corrupt an accepted release or append-only ledger.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.token = uuid.uuid4().hex
        self._held = False

    def acquire(self) -> "ExclusiveFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = canonical_json_bytes(
            {
                "pid": os.getpid(),
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "token": self.token,
            }
        )
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise LockHeldError(f"lock already held: {self.path}") from exc
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LockHeldError(f"cannot prove lock ownership: {self.path}") from exc
        if content.get("token") != self.token:
            raise LockHeldError(f"lock ownership changed: {self.path}")
        self.path.unlink()
        self._held = False

    def __enter__(self) -> "ExclusiveFileLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

