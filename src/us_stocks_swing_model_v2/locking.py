from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common import canonical_json_bytes, reject_link, require_contained_path
from .errors import ContractError, LockHeldError


def _open_exclusive_lock(path: Path) -> int:
    if os.name != "nt":
        return os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000 | 0x40000000 | 0x00010000,  # READ | WRITE | DELETE
        0x00000001 | 0x00000002 | 0x00000004,  # shared read/write/delete
        None,
        1,  # CREATE_NEW
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(error, "lock already exists", str(path))
        raise ctypes.WinError(error)
    return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)


def _mark_open_file_for_deletion(descriptor: int) -> None:
    """Ask Windows to delete the exact open file object when its handle closes."""

    if os.name != "nt":
        raise OSError("open-handle deletion is Windows-only")
    import ctypes
    import msvcrt

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_int)]

    disposition = _FileDispositionInfo(1)
    handle = msvcrt.get_osfhandle(descriptor)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    success = kernel32.SetFileInformationByHandle(
        ctypes.c_void_p(handle),
        ctypes.c_int(4),  # FileDispositionInfo
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    )
    if not success:
        raise ctypes.WinError()


class ExclusiveFileLock:
    """A fail-closed, non-stealing one-writer lock.

    Stale locks are never guessed from age. Recovery requires a deliberate
    operator action because automatically stealing a slow writer's lock can
    corrupt an accepted release or append-only ledger.
    """

    def __init__(self, path: Path, *, allowed_root: Path):
        self.path = Path(path)
        self.allowed_root = Path(allowed_root)
        if not self.path.is_absolute() or not self.allowed_root.is_absolute():
            raise ContractError("lock path and approved root must be absolute")
        self.token = uuid.uuid4().hex
        self._held = False
        self._descriptor: int | None = None
        self._identity: tuple[int, int] | None = None

    @staticmethod
    def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
        return (metadata.st_dev, metadata.st_ino)

    def _authenticate_path(self, *, must_exist: bool) -> None:
        require_contained_path(
            self.path,
            self.allowed_root,
            must_exist=must_exist,
        )
        current = self.allowed_root
        reject_link(current)
        for part in self.path.relative_to(self.allowed_root).parts:
            current = current / part
            reject_link(current)

    def acquire(self) -> "ExclusiveFileLock":
        self.allowed_root.mkdir(parents=True, exist_ok=True)
        require_contained_path(self.allowed_root, self.allowed_root)
        require_contained_path(
            self.path.parent,
            self.allowed_root,
            must_exist=False,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        require_contained_path(self.path.parent, self.allowed_root)
        self._authenticate_path(must_exist=False)
        payload = canonical_json_bytes(
            {
                "pid": os.getpid(),
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "token": self.token,
            }
        )
        try:
            descriptor = _open_exclusive_lock(self.path)
        except FileExistsError as exc:
            raise LockHeldError(f"lock already held: {self.path}") from exc
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
            descriptor_metadata = os.fstat(descriptor)
            path_metadata = os.stat(self.path, follow_symlinks=False)
            if (
                not stat.S_ISREG(descriptor_metadata.st_mode)
                or descriptor_metadata.st_nlink != 1
                or self._file_identity(descriptor_metadata)
                != self._file_identity(path_metadata)
            ):
                raise LockHeldError(f"cannot authenticate acquired lock identity: {self.path}")
        except Exception:
            try:
                descriptor_identity = self._file_identity(os.fstat(descriptor))
                path_metadata = os.stat(self.path, follow_symlinks=False)
                remove_created = (
                    self._file_identity(path_metadata) == descriptor_identity
                    and path_metadata.st_nlink == 1
                )
            except OSError:
                remove_created = False
            os.close(descriptor)
            if remove_created:
                try:
                    self.path.unlink()
                except OSError:
                    pass
            raise
        self._descriptor = descriptor
        self._identity = self._file_identity(descriptor_metadata)
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        try:
            self._authenticate_path(must_exist=True)
            assert self._descriptor is not None
            assert self._identity is not None
            descriptor_metadata = os.fstat(self._descriptor)
            path_metadata = os.stat(self.path, follow_symlinks=False)
            if (
                self._file_identity(descriptor_metadata) != self._identity
                or self._file_identity(path_metadata) != self._identity
                or path_metadata.st_nlink != 1
            ):
                raise LockHeldError(f"lock pathname identity changed: {self.path}")
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            content = json.loads(os.read(self._descriptor, 65536).decode("utf-8"))
        except LockHeldError:
            self._close_without_unlink()
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ContractError) as exc:
            self._close_without_unlink()
            raise LockHeldError(f"cannot prove lock ownership: {self.path}") from exc
        if content.get("token") != self.token:
            self._close_without_unlink()
            raise LockHeldError(f"lock ownership changed: {self.path}")
        if os.name == "nt":
            try:
                _mark_open_file_for_deletion(self._descriptor)
            except OSError as exc:
                self._close_without_unlink()
                raise LockHeldError(
                    f"cannot retire authenticated lock handle: {self.path}"
                ) from exc
            os.close(self._descriptor)
            self._descriptor = None
            self._held = False
            self._identity = None
            return

        # POSIX has no portable unlink-by-handle operation. Atomically move the
        # authenticated pathname to an unpredictable retired name while the
        # descriptor remains open, authenticate the moved identity, and retain
        # it. Never unlink a shared pathname after closing the descriptor.
        retired = self.path.with_name(
            f".released-{self.path.name}-{self.token}-{uuid.uuid4().hex}"
        )
        try:
            require_contained_path(retired, self.allowed_root, must_exist=False)
            os.replace(self.path, retired)
            retired_metadata = os.stat(retired, follow_symlinks=False)
            if (
                self._file_identity(retired_metadata) != self._identity
                or retired_metadata.st_nlink != 1
            ):
                raise LockHeldError(f"lock pathname identity changed: {self.path}")
        except LockHeldError:
            self._close_without_unlink()
            raise
        except (OSError, ContractError) as exc:
            self._close_without_unlink()
            raise LockHeldError(
                f"cannot retire authenticated lock pathname: {self.path}"
            ) from exc
        os.close(self._descriptor)
        self._descriptor = None
        self._held = False
        self._identity = None

    def _close_without_unlink(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
        self._descriptor = None
        self._identity = None
        self._held = False

    def __enter__(self) -> "ExclusiveFileLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
