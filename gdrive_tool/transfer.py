"""Streaming downloads and uploads for Google Drive mounted with GVFS."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

from .exceptions import (
    AmbiguousDuplicateError,
    GDriveError,
    RemoteNotFoundError,
    TransferError,
)
from .models import TransferProgress, TransferResult
from .mount import MountManager, with_gvfs_retry

DuplicatePolicy = Literal["skip", "rename", "overwrite", "ask"]
ProgressCallback = Callable[[TransferProgress], None]
CHUNK_SIZE = 4 * 1024 * 1024
_MISSING_ERRORS = ("no such file", "not found", "does not exist")
_CONNECTION_ERRORS = (
    "not connected",
    "connection is closed",
    "transport endpoint is not connected",
)
_ATTRIBUTE_PATTERN = re.compile(
    r"^\s*(standard::size|time::modified|standard::content-type):\s*(.*?)\s*$",
    re.MULTILINE,
)
LOGGER = logging.getLogger("gdrive_tool.transfer")


def iter_chunks(file_obj: BinaryIO, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    """Yield a file in bounded-size chunks until EOF."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be greater than zero")
    while chunk := file_obj.read(chunk_size):
        yield chunk


@contextmanager
def atomic_remote_write(
    target: Path,
    *,
    replace: Callable[[Path, Path], None] | None = None,
) -> Iterator[Path]:
    """Write to a sibling .part file and rename it only after success."""
    part_path = target.with_name(f".{target.name}.part")
    try:
        part_path.unlink(missing_ok=True)
        yield part_path
        if replace is None:
            part_path.replace(target)
        else:
            replace(part_path, target)
    except BaseException:
        try:
            part_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            LOGGER.warning(
                "Could not remove partial file %s: %s", part_path, cleanup_error
            )
        raise


@contextmanager
def _atomic_local_write(
    target: Path,
    *,
    replace: Callable[[Path, Path], None] | None = None,
) -> Iterator[Path]:
    """Create a local sibling temporary file and atomically replace the target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part_path = target.with_name(f".{target.name}.part")
    try:
        part_path.unlink(missing_ok=True)
        yield part_path
        if replace is None:
            part_path.replace(target)
        else:
            replace(part_path, target)
    except BaseException:
        try:
            part_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            LOGGER.warning(
                "Could not remove partial file %s: %s", part_path, cleanup_error
            )
        raise


class TransferEngine:
    """Perform atomic, retried file transfers through a selected GVFS mount."""

    def __init__(
        self,
        manager: MountManager | None = None,
        *,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be greater than zero")
        self.manager = manager or MountManager()
        self.chunk_size = chunk_size

    def reconnect(self) -> None:
        """Delegate a reconnection attempt to the active mount manager."""
        self.manager.reconnect()

    @with_gvfs_retry(attempts=3, initial_delay=0.5)
    def _run_gio(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run GIO and map connection or command failures to domain errors."""
        try:
            result = subprocess.run(
                ["gio", *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            if isinstance(error, OSError) and error.errno in {
                errno.ENOTCONN,
                errno.ETIMEDOUT,
                errno.ECONNRESET,
                errno.EAGAIN,
                errno.EIO,
            }:
                raise
            if isinstance(error, subprocess.TimeoutExpired):
                raise OSError(errno.ETIMEDOUT, str(error)) from error
            raise TransferError(
                f"Could not run gio {' '.join(arguments)}: {error}"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            normalized = detail.casefold()
            if any(message in normalized for message in _CONNECTION_ERRORS):
                raise OSError(errno.ENOTCONN, detail)
            if any(message in normalized for message in _MISSING_ERRORS):
                raise RemoteNotFoundError(detail or "Drive path was not found")
            raise TransferError(f"gio {' '.join(arguments)} failed: {detail}")
        return result

    @with_gvfs_retry(attempts=3, initial_delay=0.5)
    def _copy_to_part(
        self,
        source: Path,
        part_path: Path,
        total_bytes: int,
        progress: ProgressCallback | None,
    ) -> tuple[int, str]:
        """Copy and hash a file in one pass, restarting from byte zero on retry."""
        digest = hashlib.sha256()
        copied = 0
        started = time.monotonic()
        with source.open("rb") as source_file, part_path.open("wb") as target_file:
            for chunk in iter_chunks(source_file, self.chunk_size):
                target_file.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
                if progress is not None:
                    elapsed = max(time.monotonic() - started, 0.000001)
                    speed = copied / elapsed
                    remaining = max(total_bytes - copied, 0)
                    progress(
                        TransferProgress(
                            bytes_transferred=copied,
                            total_bytes=total_bytes,
                            bytes_per_second=speed,
                            eta_seconds=remaining / speed if speed > 0 else None,
                        )
                    )
            target_file.flush()
        if copied != total_bytes:
            raise TransferError(
                f"Source size changed during transfer: expected {total_bytes}, copied {copied}"
            )
        return copied, digest.hexdigest()

    @with_gvfs_retry(attempts=3, initial_delay=0.5)
    def _replace(self, source: Path, destination: Path) -> None:
        """Commit a completed sibling .part file using the filesystem rename."""
        source.replace(destination)

    @with_gvfs_retry(attempts=3, initial_delay=0.5)
    def _sha256_file(self, path: Path) -> tuple[int, str]:
        """Read a completed file once to verify its byte count and hash."""
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as file_obj:
            for chunk in iter_chunks(file_obj, self.chunk_size):
                digest.update(chunk)
                size += len(chunk)
        return size, digest.hexdigest()

    def _remote_info(self, path: Path) -> dict[str, str]:
        """Read one remote file's metadata with one gio-info invocation."""
        result = self._run_gio(
            "info",
            "-a",
            "standard::size,time::modified,standard::content-type",
            str(path),
        )
        return dict(_ATTRIBUTE_PATTERN.findall(result.stdout))

    def _remote_names(self, directory: Path) -> set[str]:
        """Read names from one remote directory listing."""
        result = self._run_gio("list", "-a", "standard::name", str(directory))
        names: set[str] = set()
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            name = fields[0].strip()
            if name:
                names.add(name)
        return names

    def _ensure_remote_directory(self, directory: Path) -> None:
        """Create missing parent directories through GIO."""
        try:
            self._run_gio("info", str(directory))
        except RemoteNotFoundError:
            self._run_gio("mkdir", "-p", str(directory))

    @staticmethod
    def _remote_size(metadata: dict[str, str], path: Path) -> int:
        """Return validated file size from GIO metadata."""
        try:
            size = int(metadata.get("standard::size", "0"))
        except ValueError as error:
            raise TransferError(f"Invalid size metadata for {path}") from error
        return max(size, 0)

    @staticmethod
    def _remote_mtime(metadata: dict[str, str]) -> float | None:
        """Return modified time when GIO provides it."""
        value = metadata.get("time::modified")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _ensure_transferable(path: Path, size: int, content_type: str | None) -> None:
        """Reject Google-native documents and unsupported empty placeholders."""
        google_native = bool(
            content_type and content_type.startswith("application/vnd.google-apps.")
        )
        google_native = google_native or path.name.lower().endswith(
            (".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform", ".gmap")
        )
        if google_native or size == 0:
            raise TransferError(
                f"{path.name!r} is a Google-native document or empty placeholder; "
                "it cannot be copied as a regular file."
            )

    @staticmethod
    def _same_file(
        local_size: int,
        local_mtime: float,
        remote_size: int,
        remote_mtime: float | None,
    ) -> bool:
        """Compare size and second-resolution mtime for compatible existing files."""
        return (
            local_size == remote_size
            and remote_mtime is not None
            and abs(local_mtime - remote_mtime) < 1.0
        )

    @staticmethod
    def _choose_policy(policy: DuplicatePolicy) -> DuplicatePolicy:
        """Resolve the ask policy only after a destination conflict is found."""
        match policy:
            case "skip" | "rename" | "overwrite":
                return policy
            case "ask":
                if not sys.stdin.isatty():
                    raise AmbiguousDuplicateError(
                        "Duplicate policy 'ask' requires an interactive terminal."
                    )
                while True:
                    answer = input(
                        "Destination exists: [s]kip, [r]ename, [o]verwrite? "
                    )
                    match answer.strip().lower():
                        case "s" | "skip":
                            return "skip"
                        case "r" | "rename":
                            return "rename"
                        case "o" | "overwrite":
                            return "overwrite"
                        case _:
                            print("Choose skip, rename, or overwrite.")
        raise ValueError(f"Unsupported duplicate policy: {policy}")

    @staticmethod
    def _renamed_local_path(path: Path) -> Path:
        """Find the next unused local name using the conventional (N) suffix."""
        index = 1
        while True:
            candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
            if not candidate.exists():
                return candidate
            index += 1

    @staticmethod
    def _renamed_remote_path(path: Path, names: set[str]) -> Path:
        """Find the next unused remote name from one directory listing."""
        index = 1
        while True:
            candidate_name = f"{path.stem} ({index}){path.suffix}"
            if candidate_name not in names:
                return path.with_name(candidate_name)
            index += 1

    def download(
        self,
        remote_path: str,
        local_path: str | Path | None = None,
        *,
        verify: bool = False,
        dry_run: bool = False,
        on_duplicate: DuplicatePolicy = "skip",
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        """Download one virtual Drive file to a local path atomically."""
        resolved = self.manager.resolve(remote_path)
        source = resolved.local_path
        destination = (
            Path(local_path) if local_path is not None else Path.cwd() / source.name
        )
        policy = on_duplicate
        try:
            metadata = self._remote_info(source)
            size = self._remote_size(metadata, source)
            if metadata.get("standard::content-type") == "inode/directory":
                raise TransferError(
                    f"{remote_path!r} is a directory; this command transfers one file at a time."
                )
            self._ensure_transferable(
                source, size, metadata.get("standard::content-type")
            )
            mtime = self._remote_mtime(metadata)
            if destination.exists():
                local_stat = destination.stat()
                if not stat.S_ISREG(local_stat.st_mode):
                    raise TransferError(
                        f"Download destination is not a regular file: {destination}"
                    )
                if self._same_file(
                    local_stat.st_size, local_stat.st_mtime, size, mtime
                ):
                    return TransferResult(
                        remote_path, str(destination), 0, skipped=True
                    )
                policy = self._choose_policy(policy)
                match policy:
                    case "skip":
                        return TransferResult(
                            remote_path, str(destination), 0, skipped=True
                        )
                    case "rename":
                        destination = self._renamed_local_path(destination)
                    case "overwrite":
                        LOGGER.info("Replacing existing local file %s", destination)
                    case "ask":
                        raise RuntimeError("duplicate policy was not resolved")
            if dry_run:
                return TransferResult(remote_path, str(destination), 0, dry_run=True)
            with _atomic_local_write(destination, replace=self._replace) as part_path:
                copied, digest = self._copy_to_part(source, part_path, size, progress)
                if mtime is not None:
                    os.utime(part_path, (mtime, mtime))
                if verify:
                    verified_size, verified_digest = self._sha256_file(part_path)
                    if verified_size != size or verified_digest != digest:
                        raise TransferError(f"Verification failed for {remote_path}")
            return TransferResult(
                remote_path,
                str(destination),
                copied,
                sha256=digest,
            )
        except (RemoteNotFoundError, TransferError, AmbiguousDuplicateError):
            raise
        except OSError as error:
            raise TransferError(
                f"Download failed for {remote_path}: {error}"
            ) from error

    def upload(
        self,
        local_path: str | Path,
        remote_path: str | None = None,
        *,
        verify: bool = False,
        dry_run: bool = False,
        on_duplicate: DuplicatePolicy = "skip",
        progress: ProgressCallback | None = None,
    ) -> TransferResult:
        """Upload one local file to the Drive mount using a .part file."""
        source = Path(local_path)
        target_virtual = remote_path if remote_path is not None else source.name
        resolved = self.manager.resolve(target_virtual)
        destination = resolved.local_path
        policy = on_duplicate
        try:
            source_stat = source.stat()
            if not stat.S_ISREG(source_stat.st_mode):
                raise TransferError(f"Upload source is not a regular file: {source}")
            self._ensure_transferable(source, source_stat.st_size, None)
            self._ensure_remote_directory(destination.parent)
            names = self._remote_names(destination.parent)
            target_exists = destination.name in names
            if target_exists:
                metadata = self._remote_info(destination)
                remote_size = self._remote_size(metadata, destination)
                remote_mtime = self._remote_mtime(metadata)
                if self._same_file(
                    source_stat.st_size,
                    source_stat.st_mtime,
                    remote_size,
                    remote_mtime,
                ):
                    return TransferResult(str(source), target_virtual, 0, skipped=True)
                policy = self._choose_policy(policy)
                match policy:
                    case "skip":
                        return TransferResult(
                            str(source), target_virtual, 0, skipped=True
                        )
                    case "rename":
                        destination = self._renamed_remote_path(destination, names)
                        target_virtual = (
                            f"{resolved.virtual_path.rsplit('/', 1)[0]}/"
                            f"{destination.name}"
                            if "/" in resolved.virtual_path
                            else destination.name
                        )
                    case "overwrite":
                        LOGGER.info("Replacing existing Drive file %s", target_virtual)
                    case "ask":
                        raise RuntimeError("duplicate policy was not resolved")
            if dry_run:
                return TransferResult(str(source), target_virtual, 0, dry_run=True)
            with atomic_remote_write(destination, replace=self._replace) as part_path:
                copied, digest = self._copy_to_part(
                    source, part_path, source_stat.st_size, progress
                )
                if verify:
                    verified_size, verified_digest = self._sha256_file(part_path)
                    if (
                        verified_size != source_stat.st_size
                        or verified_digest != digest
                    ):
                        raise TransferError(f"Verification failed for {target_virtual}")
            return TransferResult(
                str(source),
                target_virtual,
                copied,
                sha256=digest,
            )
        except (RemoteNotFoundError, TransferError, AmbiguousDuplicateError):
            raise
        except OSError as error:
            raise TransferError(f"Upload failed for {source}: {error}") from error


def download(
    remote_path: str,
    local_path: str | Path | None = None,
    *,
    verify: bool = False,
    dry_run: bool = False,
    on_duplicate: DuplicatePolicy = "skip",
    progress: ProgressCallback | None = None,
    manager: MountManager | None = None,
) -> TransferResult:
    """Download a Drive file; report expected I/O failures without raising."""
    source_label = remote_path
    destination_label = (
        str(local_path)
        if local_path is not None
        else str(Path.cwd() / Path(remote_path).name)
    )
    try:
        return TransferEngine(manager).download(
            remote_path,
            local_path,
            verify=verify,
            dry_run=dry_run,
            on_duplicate=on_duplicate,
            progress=progress,
        )
    except (GDriveError, OSError) as error:
        print(f"Download failed; continuing: {error}", file=sys.stderr)
        return TransferResult(
            source=source_label,
            destination=destination_label,
            bytes_transferred=0,
            success=False,
            error=str(error),
        )


def upload(
    local_path: str | Path,
    remote_path: str | None = None,
    *,
    verify: bool = False,
    dry_run: bool = False,
    on_duplicate: DuplicatePolicy = "skip",
    progress: ProgressCallback | None = None,
    manager: MountManager | None = None,
) -> TransferResult:
    """Upload a local file; report expected I/O failures without raising."""
    source_label = str(local_path)
    destination_label = remote_path or Path(local_path).name
    try:
        return TransferEngine(manager).upload(
            local_path,
            remote_path,
            verify=verify,
            dry_run=dry_run,
            on_duplicate=on_duplicate,
            progress=progress,
        )
    except (GDriveError, OSError) as error:
        print(f"Upload failed; continuing: {error}", file=sys.stderr)
        return TransferResult(
            source=source_label,
            destination=destination_label,
            bytes_transferred=0,
            success=False,
            error=str(error),
        )


def load_file(
    remote_path: str,
    local_path: str | Path | None = None,
    *,
    verify: bool = False,
    on_duplicate: DuplicatePolicy = "skip",
    manager: MountManager | None = None,
) -> Path | None:
    """Download a file and return its local path, or ``None`` after an I/O failure."""
    result = download(
        remote_path,
        local_path,
        verify=verify,
        on_duplicate=on_duplicate,
        manager=manager,
    )
    return Path(result.destination) if result.success else None
