"""Immutable value objects shared by the package."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DriveEntry:
    """A single entry returned by a GVFS directory listing."""

    name: str
    path: str
    size: int
    mtime: float | None
    content_type: str | None
    duplicate_index: int = 1

    @property
    def is_directory(self) -> bool:
        """Return whether the entry represents a directory."""
        return self.content_type == "inode/directory"

    @property
    def is_google_native(self) -> bool:
        """Return whether Google owns the document representation."""
        return (
            bool(
                self.content_type
                and self.content_type.startswith("application/vnd.google-apps.")
            )
            or self.name.lower().endswith(
                (".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform", ".gmap")
            )
            or self.size == 0
        )


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A virtual Drive path and its mounted filesystem representation."""

    virtual_path: str
    local_path: Path
    uri: str | None = None
    account: str | None = None


@dataclass(frozen=True, slots=True)
class TransferResult:
    """Summary of a completed or simulated transfer."""

    source: str
    destination: str
    bytes_transferred: int
    sha256: str | None = None
    skipped: bool = False
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class TransferProgress:
    """Progress snapshot emitted while streaming a file."""

    bytes_transferred: int
    total_bytes: int
    bytes_per_second: float
    eta_seconds: float | None


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Persisted user preferences; tuple fields keep instances immutable."""

    default_account: str | None = None
    cache_ttl_seconds: int = 600
    favorites: tuple[str, ...] = ()
    recents: tuple[str, ...] = ()
    shortcuts: tuple[tuple[str, str], ...] = ()

    def shortcut_map(self) -> dict[str, str]:
        """Return a mutable lookup copy of configured named shortcuts."""
        return dict(self.shortcuts)
