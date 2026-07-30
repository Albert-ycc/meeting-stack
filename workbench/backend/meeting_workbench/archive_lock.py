from __future__ import annotations

import fcntl
import os
from pathlib import Path


class ArchiveLock:
    """Advisory cross-process lock shared by archive readers and publishers."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._descriptor: int | None = None

    def __enter__(self) -> ArchiveLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
