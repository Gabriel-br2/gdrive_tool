"""GVFS mount discovery, virtual path resolution, and retry helpers."""

from __future__ import annotations

import errno
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import ParamSpec, TypeVar, overload
from urllib.parse import unquote, urlparse

from .config import load_config, save_config
from .exceptions import InvalidPathError, MountNotFoundError
from .models import AppConfig, ResolvedPath

P = ParamSpec("P")
R = TypeVar("R")
LOGGER = logging.getLogger("gdrive_tool.mount")
_RETRYABLE_ERRNOS = {
    errno.ENOTCONN,
    errno.ETIMEDOUT,
    errno.ECONNRESET,
    errno.EAGAIN,
    errno.EIO,
}
_MOUNTPOINT_PATTERN = re.compile(r"^\s*Mountpoint:\s*(.+?)\s*$")
_ROOT_PATTERN = re.compile(r"^\s*Root:\s*(\S+)\s*$")
_DRIVE_URI_PATTERN = re.compile(r"(google-drive://[^\s]+)")


@overload
def with_gvfs_retry(
    function: Callable[P, R],
    *,
    attempts: int = 3,
    initial_delay: float = 0.25,
) -> Callable[P, R]: ...


@overload
def with_gvfs_retry(
    function: None = None,
    *,
    attempts: int = 3,
    initial_delay: float = 0.25,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def with_gvfs_retry(
    function: Callable[P, R] | None = None,
    *,
    attempts: int = 3,
    initial_delay: float = 0.25,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry transient disconnected-GVFS I/O, reconnecting a bound manager."""
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    if initial_delay < 0:
        raise ValueError("initial_delay must be non-negative")

    def decorate(target: Callable[P, R]) -> Callable[P, R]:
        @wraps(target)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(attempts):
                try:
                    return target(*args, **kwargs)
                except OSError as error:
                    if error.errno not in _RETRYABLE_ERRNOS or attempt + 1 >= attempts:
                        raise
                    owner = args[0] if args else None
                    reconnect = getattr(owner, "reconnect", None)
                    if callable(reconnect):
                        reconnect()
                    delay = initial_delay * (2**attempt)
                    LOGGER.warning(
                        "GVFS disconnected; retrying operation",
                        extra={"attempt": attempt + 1, "delay_seconds": delay},
                    )
                    time.sleep(delay)
            raise RuntimeError("unreachable retry state")

        return wrapped

    if function is None:
        return decorate
    return decorate(function)


class MountManager:
    """Discover mounted Drive roots and translate virtual paths to GVFS paths."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.config = config if config is not None else load_config()
        self.config_path = config_path
        self.mountpoint: Path | None = None
        self.mount_uri: str | None = None
        self.account: str | None = None

    @staticmethod
    def _gio_mount_records() -> list[tuple[Path, str | None]]:
        """Parse mountpoints and root URIs from `gio mount -l` output."""
        try:
            result = subprocess.run(
                ["gio", "mount", "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        records: list[tuple[Path, str | None]] = []
        current_root: str | None = None
        for line in result.stdout.splitlines():
            root_match = _ROOT_PATTERN.match(line)
            if root_match:
                current_root = root_match.group(1)
                continue
            mount_match = _MOUNTPOINT_PATTERN.match(line)
            if mount_match:
                mountpoint = Path(mount_match.group(1))
                if "google-drive:" in str(mountpoint):
                    records.append((mountpoint, current_root))
                current_root = None
        return records

    @staticmethod
    def _gio_drive_uris() -> tuple[str, ...]:
        """Read Google Drive root URIs from GIO's mount listing."""
        try:
            result = subprocess.run(
                ["gio", "mount", "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if result.returncode != 0:
            return ()
        uris = (
            match.group(1).rstrip("),")
            for line in result.stdout.splitlines()
            if (match := _DRIVE_URI_PATTERN.search(line)) is not None
        )
        return tuple(dict.fromkeys(uris))

    @staticmethod
    def _gvfs_mountpoints() -> list[Path]:
        """Find Google Drive mount directories below the current user's GVFS."""
        gvfs_root = Path(f"/run/user/{os.getuid()}/gvfs")
        try:
            return sorted(
                path
                for path in gvfs_root.iterdir()
                if path.is_dir() and path.name.startswith("google-drive:")
            )
        except OSError:
            return []

    def discover(self) -> tuple[Path, ...]:
        """Return detected Google Drive mountpoints, preferring GVFS directly."""
        direct = self._gvfs_mountpoints()
        listed = [path for path, _ in self._gio_mount_records()]
        return tuple(dict.fromkeys((*direct, *listed)))

    def _select_mount(
        self, mountpoints: tuple[Path, ...], *, interactive: bool
    ) -> Path:
        """Choose the configured account or ask the user to select one."""
        configured = self.config.default_account
        if configured:
            configured_match = next(
                (
                    mountpoint
                    for mountpoint in mountpoints
                    if configured == mountpoint.name
                    or configured in str(mountpoint)
                    or configured.split("@", maxsplit=1)[0] in mountpoint.name
                ),
                None,
            )
            if configured_match is not None:
                return configured_match
        if len(mountpoints) == 1:
            return mountpoints[0]
        if not mountpoints:
            raise MountNotFoundError(
                "Google Drive is not mounted. Run `gio mount google-drive://` "
                "and try again."
            )
        if not interactive:
            raise MountNotFoundError(
                "Multiple Google Drive accounts are mounted; configure default_account "
                "or run the command interactively."
            )
        for index, mountpoint in enumerate(mountpoints, start=1):
            print(f"{index}. {mountpoint.name}")
        while True:
            choice = input("Select Google Drive account: ").strip()
            if choice.isdecimal() and 1 <= int(choice) <= len(mountpoints):
                selected = mountpoints[int(choice) - 1]
                updated = AppConfig(
                    default_account=selected.name,
                    cache_ttl_seconds=self.config.cache_ttl_seconds,
                    favorites=self.config.favorites,
                    recents=self.config.recents,
                    shortcuts=self.config.shortcuts,
                )
                if self.config_path is None:
                    save_config(updated)
                else:
                    save_config(updated, self.config_path)
                self.config = updated
                return selected
            print(f"Enter a number from 1 to {len(mountpoints)}.")

    def ensure_mounted(self, *, interactive: bool = False) -> Path:
        """Locate and select the mounted Drive, raising if none is available."""
        mounts = self.discover()
        self.mountpoint = self._select_mount(mounts, interactive=interactive)
        for mountpoint, root in self._gio_mount_records():
            if mountpoint == self.mountpoint:
                self.mount_uri = root
                break
        if self.mount_uri is None:
            for uri in self._gio_drive_uris():
                uri_account = unquote(urlparse(uri).netloc).split("@", maxsplit=1)[0]
                if uri_account and uri_account in self.mountpoint.name:
                    self.mount_uri = uri
                    break
            if self.mount_uri is None:
                uris = self._gio_drive_uris()
                if len(uris) == 1 and len(self.discover()) == 1:
                    self.mount_uri = uris[0]
        self.account = self.mountpoint.name
        return self.mountpoint

    def reconnect(self) -> None:
        """Ask GIO to reconnect the selected mount when possible."""
        if self.mount_uri is None:
            mount_uri = "google-drive://"
        else:
            mount_uri = self.mount_uri
        try:
            result = subprocess.run(
                ["gio", "mount", mount_uri],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.warning("Could not reconnect GVFS mount: %s", error)
            return
        if result.returncode != 0:
            LOGGER.warning("gio mount failed: %s", result.stderr.strip())

    @staticmethod
    def _validate_relative(value: str) -> PurePosixPath:
        """Normalize a virtual path and reject traversal above the Drive root."""
        normalized = value.replace("\\", "/").strip("/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or any(part in ("..", "") for part in path.parts):
            raise InvalidPathError(f"Invalid Drive-relative path: {value!r}")
        return path

    def resolve(self, value: str, *, interactive: bool = False) -> ResolvedPath:
        """Resolve relative, shortcut, or Google Drive URI paths."""
        mountpoint = self.mountpoint or self.ensure_mounted(interactive=interactive)
        original = value
        account: str | None = self.account
        if value.startswith("@"):
            shortcut, separator, remainder = value[1:].partition("/")
            shortcuts = self.config.shortcut_map()
            if shortcut not in shortcuts:
                raise InvalidPathError(f"Unknown Drive shortcut: @{shortcut}")
            value = "/".join(part for part in (shortcuts[shortcut], remainder) if part)
        if value.startswith("google-drive://"):
            parsed = urlparse(value)
            uri_account = unquote(parsed.netloc)
            if uri_account and self.mount_uri and uri_account not in self.mount_uri:
                raise MountNotFoundError(
                    f"URI account {uri_account!r} is not the selected mounted account"
                )
            account = uri_account or account
            relative = unquote(parsed.path).lstrip("/")
        else:
            relative = value
        virtual = self._validate_relative(relative)
        local_path = mountpoint.joinpath(*virtual.parts)
        try:
            local_path.relative_to(mountpoint)
        except ValueError as error:
            raise InvalidPathError(
                f"Path escapes the Drive mount: {original!r}"
            ) from error
        virtual_value = virtual.as_posix() if virtual.parts else "."
        return ResolvedPath(
            virtual_path=virtual_value,
            local_path=local_path,
            uri=original if original.startswith("google-drive://") else None,
            account=account,
        )

    def doctor(self, *, remount: bool = False) -> dict[str, object]:
        """Report mount availability and optionally request a remount."""
        mounts = self.discover()
        report: dict[str, object] = {
            "healthy": bool(mounts),
            "mounts": [str(path) for path in mounts],
            "selected": None,
            "gio_available": self._gio_available(),
        }
        if not mounts:
            if remount:
                self.reconnect()
                mounts = self.discover()
                report["mounts"] = [str(path) for path in mounts]
                report["healthy"] = bool(mounts)
            return report
        if len(mounts) > 1 and not self.config.default_account:
            report["healthy"] = True
            report["selection_required"] = True
            return report
        selected = self.ensure_mounted()
        report["selected"] = str(selected)
        report["account"] = self.account
        if remount:
            self.reconnect()
            report["remount_requested"] = True
        return report

    @staticmethod
    def _gio_available() -> bool:
        """Return whether the GIO command-line utility can be executed."""
        try:
            subprocess.run(
                ["gio", "version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False


def resolve_path(value: str, *, interactive: bool = False) -> ResolvedPath:
    """Public convenience function for resolving a virtual Drive path."""
    return MountManager().resolve(value, interactive=interactive)
