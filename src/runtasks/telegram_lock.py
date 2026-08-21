from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from runtasks.telegram_errors import (
    PollerAlreadyRunningError,
    TelegramConfigurationError,
)


_LOCK_MODULE: Any = importlib.import_module(
    "msvcrt" if os.name == "nt" else "fcntl"
)


class PollerGuard:
    """Cross-process lock stored in the RunTasks-owned global lock directory."""

    def __init__(self, lock_path: Path) -> None:
        self._path = lock_path
        self._file: BinaryIO | None = None

    def __enter__(self) -> PollerGuard:
        self._path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        lock_file = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            _acquire_lock(lock_file)
        except OSError:
            lock_file.close()
            raise PollerAlreadyRunningError(
                "another Telegram process is already polling this bot configuration"
            ) from None
        self._file = lock_file
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is None:
            return
        try:
            _release_lock(self._file)
        except OSError:
            raise TelegramConfigurationError(
                "Telegram single-poller guard could not be released"
            ) from None
        finally:
            self._file.close()
            self._file = None


def _acquire_lock(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        lock_file.seek(0)
        if lock_file.read(1) == b"":
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        _LOCK_MODULE.locking(
            lock_file.fileno(),
            _LOCK_MODULE.LK_NBLCK,
            1,
        )
    else:
        _LOCK_MODULE.flock(
            lock_file.fileno(),
            _LOCK_MODULE.LOCK_EX | _LOCK_MODULE.LOCK_NB,
        )


def _release_lock(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        lock_file.seek(0)
        _LOCK_MODULE.locking(
            lock_file.fileno(),
            _LOCK_MODULE.LK_UNLCK,
            1,
        )
    else:
        _LOCK_MODULE.flock(lock_file.fileno(), _LOCK_MODULE.LOCK_UN)
