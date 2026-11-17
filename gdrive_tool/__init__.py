"""Python API for Google Drive mounted through Linux GVFS."""

from __future__ import annotations

from .models import (
    AppConfig,
    DriveEntry,
    ResolvedPath,
    TransferProgress,
    TransferResult,
)
from .mount import MountManager, resolve_path, with_gvfs_retry
from .transfer import (
    DuplicatePolicy,
    TransferEngine,
    atomic_remote_write,
    download,
    iter_chunks,
    load_file,
    upload,
)


def doctor(*, remount: bool = False) -> dict[str, object]:
    """Return a diagnostic report for the current GVFS mount."""
    return MountManager().doctor(remount=remount)


__all__ = [
    "AppConfig",
    "DuplicatePolicy",
    "DriveEntry",
    "MountManager",
    "ResolvedPath",
    "TransferEngine",
    "TransferProgress",
    "TransferResult",
    "atomic_remote_write",
    "doctor",
    "download",
    "iter_chunks",
    "load_file",
    "resolve_path",
    "upload",
    "with_gvfs_retry",
]
