"""Domain exceptions and process exit codes for gdrive-tool."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Stable process exit codes used by the CLI."""

    SUCCESS = 0
    GENERAL_ERROR = 1
    MOUNT_NOT_FOUND = 2
    REMOTE_NOT_FOUND = 3
    AMBIGUOUS_DUPLICATE = 4
    INVALID_ARGUMENT = 5
    CONFIGURATION_ERROR = 6
    CACHE_ERROR = 7


class GDriveError(Exception):
    """Base class for expected application errors."""

    exit_code: ExitCode = ExitCode.GENERAL_ERROR

    def __init__(self, message: str, *, exit_code: ExitCode | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class MountNotFoundError(GDriveError):
    """Raised when no mounted Google Drive is available."""

    exit_code = ExitCode.MOUNT_NOT_FOUND


class RemoteNotFoundError(GDriveError):
    """Raised when a requested virtual path does not exist."""

    exit_code = ExitCode.REMOTE_NOT_FOUND


class AmbiguousDuplicateError(GDriveError):
    """Raised when an unqualified filename identifies multiple entries."""

    exit_code = ExitCode.AMBIGUOUS_DUPLICATE


class InvalidPathError(GDriveError):
    """Raised when a virtual path is malformed or escapes the Drive root."""

    exit_code = ExitCode.INVALID_ARGUMENT


class ConfigurationError(GDriveError):
    """Raised for invalid or unreadable user configuration."""

    exit_code = ExitCode.CONFIGURATION_ERROR


class CacheError(GDriveError):
    """Raised when the SQLite metadata cache cannot be used."""

    exit_code = ExitCode.CACHE_ERROR


class TransferError(GDriveError):
    """Raised when a file cannot be transferred safely."""

    exit_code = ExitCode.GENERAL_ERROR
