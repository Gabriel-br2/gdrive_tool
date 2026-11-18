"""Unit tests for streaming transfer behavior."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from gdrive_tool.exceptions import (
    MountNotFoundError,
    RemoteNotFoundError,
    TransferError,
)
from gdrive_tool.models import AppConfig, TransferProgress
from gdrive_tool.mount import MountManager, with_gvfs_retry
from gdrive_tool.transfer import (
    TransferEngine,
    download,
    iter_chunks,
    load_file,
    upload,
)


def make_engine(tmp_path: Path, chunk_size: int = 4) -> tuple[TransferEngine, Path]:
    """Create an engine backed by a temporary directory instead of GVFS."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    manager = MountManager(AppConfig())
    manager.mountpoint = drive_root
    manager.mount_uri = "google-drive://test-account/"
    manager.account = "test-account"
    return TransferEngine(manager, chunk_size=chunk_size), drive_root


def use_fake_directory_listing(
    monkeypatch: pytest.MonkeyPatch,
    engine: TransferEngine,
) -> None:
    """Replace GIO parent checks with the temporary filesystem."""
    monkeypatch.setattr(
        engine,
        "_ensure_remote_directory",
        lambda path: path.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        engine,
        "_remote_names",
        lambda path: {child.name for child in path.iterdir()},
    )

    def fake_run_gio(*arguments: str) -> object:
        if arguments[0] == "copy":
            source = Path(arguments[-2])
            destination_uri = urlparse(arguments[-1])
            destination = engine.manager.mountpoint / unquote(
                destination_uri.path.lstrip("/")
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if "--backup" in arguments and destination.exists():
                shutil.copyfile(
                    destination, destination.with_name(destination.name + "~")
                )
            shutil.copyfile(source, destination)
        return object()

    monkeypatch.setattr(engine, "_run_gio", fake_run_gio)


def metadata_for(
    path: Path,
    content_type: str = "application/octet-stream",
) -> dict[str, str]:
    """Return GIO-like metadata for a temporary file."""
    file_stat = path.stat()
    return {
        "standard::size": str(file_stat.st_size),
        "time::modified": str(file_stat.st_mtime),
        "standard::content-type": content_type,
    }


def test_iter_chunks_streams_without_reading_all_at_once() -> None:
    """Chunks preserve byte order and the final short chunk."""
    assert list(iter_chunks(BytesIO(b"abcdefgh"), 3)) == [b"abc", b"def", b"gh"]


def test_iter_chunks_rejects_non_positive_chunk_size() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        list(iter_chunks(BytesIO(b"data"), 0))


def test_upload_writes_and_verifies_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, drive_root = make_engine(tmp_path)
    local_file = tmp_path / "source.bin"
    payload = b"upload payload" * 100
    local_file.write_bytes(payload)
    use_fake_directory_listing(monkeypatch, engine)

    result = engine.upload(local_file, "reports/source.bin", verify=True)

    destination = drive_root / "reports" / "source.bin"
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()


def test_ensure_remote_directory_creates_each_missing_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, drive_root = make_engine(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_run_gio(*arguments: str) -> object:
        calls.append(arguments)
        if arguments[0] == "info":
            raise RemoteNotFoundError("missing")
        return object()

    monkeypatch.setattr(engine, "_run_gio", fake_run_gio)

    engine._ensure_remote_directory(drive_root / "Projects" / "Mestrado")

    assert calls == [
        ("info", str(drive_root / "Projects")),
        ("mkdir", str(drive_root / "Projects")),
        ("info", str(drive_root / "Projects" / "Mestrado")),
        ("mkdir", str(drive_root / "Projects" / "Mestrado")),
    ]


def test_mount_manager_selects_writable_drive_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gdrive_tool.mount.subprocess.run",
        lambda *arguments, **kwargs: subprocess.CompletedProcess(
            arguments,
            0,
            stdout=(
                "GVfsSharedWithMe\t0\t(directory)\taccess::can-write=FALSE\n"
                "drive-folder-id\t0\t(directory)\taccess::can-write=TRUE\n"
            ),
            stderr="",
        ),
    )

    assert MountManager._writable_drive_root("google-drive://test-account/") == (
        "drive-folder-id"
    )


def test_mount_manager_resolves_paths_from_drive_folder(tmp_path: Path) -> None:
    manager = MountManager(AppConfig())
    manager.mountpoint = tmp_path / "mount"
    manager.drive_root = manager.mountpoint / "drive-folder-id"
    manager.mount_uri = "google-drive://test-account/"
    manager.drive_uri = "google-drive://test-account/drive-folder-id"
    manager.account = "test-account"

    resolved = manager.resolve("reports/report.csv")
    uri_resolved = manager.resolve(
        "google-drive://test-account/drive-folder-id/reports/report.csv"
    )

    assert resolved.local_path == manager.drive_root / "reports" / "report.csv"
    assert uri_resolved.local_path == resolved.local_path


def test_remote_names_uses_display_names_in_gio_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, drive_root = make_engine(tmp_path)
    monkeypatch.setattr(
        engine,
        "_run_gio",
        lambda *arguments: subprocess.CompletedProcess(
            arguments,
            0,
            stdout=(
                "file-id\t24\t(regular)\t"
                "standard::display-name=report with spaces.csv\n"
            ),
            stderr="",
        ),
    )

    assert engine._remote_names(drive_root) == {"report with spaces.csv"}


def test_download_verifies_copy_and_preserves_local_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, drive_root = make_engine(tmp_path)
    remote_file = drive_root / "report.bin"
    payload = b"download payload" * 100
    remote_file.write_bytes(payload)
    remote_mtime = 1_700_000_000.0
    os.utime(remote_file, (remote_mtime, remote_mtime))
    monkeypatch.setattr(engine, "_remote_info", lambda path: metadata_for(path))
    destination = tmp_path / "download" / "report.bin"

    result = engine.download("report.bin", destination, verify=True)

    assert destination.read_bytes() == payload
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert destination.stat().st_mtime == remote_mtime


def test_upload_skip_and_rename_duplicate_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, drive_root = make_engine(tmp_path)
    remote_dir = drive_root / "reports"
    remote_dir.mkdir()
    existing = remote_dir / "report.txt"
    existing.write_bytes(b"old")
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"new content")
    use_fake_directory_listing(monkeypatch, engine)
    monkeypatch.setattr(engine, "_remote_info", lambda path: metadata_for(path))

    skipped = engine.upload(local_file, "reports/report.txt", on_duplicate="skip")
    assert skipped.skipped
    assert existing.read_bytes() == b"old"

    renamed = engine.upload(local_file, "reports/report.txt", on_duplicate="rename")
    renamed_path = remote_dir / "report (1).txt"
    assert renamed.destination == "reports/report (1).txt"
    assert renamed_path.read_bytes() == b"new content"
    assert existing.read_bytes() == b"old"


def test_upload_overwrites_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, drive_root = make_engine(tmp_path)
    remote_dir = drive_root / "reports"
    remote_dir.mkdir()
    existing = remote_dir / "report.txt"
    existing.write_bytes(b"old")
    local_file = tmp_path / "report.txt"
    local_file.write_bytes(b"replacement")
    use_fake_directory_listing(monkeypatch, engine)
    monkeypatch.setattr(engine, "_remote_info", lambda path: metadata_for(path))

    result = engine.upload(local_file, "reports/report.txt", on_duplicate="overwrite")

    assert not result.skipped
    assert existing.read_bytes() == b"replacement"


def test_upload_rejects_empty_google_native_placeholders(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    empty_file = tmp_path / "document.gdoc"
    empty_file.touch()

    with pytest.raises(TransferError, match="Google-native"):
        engine.upload(empty_file)


def test_upload_callback_failure_occurs_after_gio_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, drive_root = make_engine(tmp_path)
    local_file = tmp_path / "source.bin"
    local_file.write_bytes(b"some bytes to stream")
    use_fake_directory_listing(monkeypatch, engine)

    def fail_progress(progress: TransferProgress) -> None:
        raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated failure"):
        engine.upload(local_file, "source.bin", progress=fail_progress)

    assert (drive_root / "source.bin").read_bytes() == local_file.read_bytes()


def test_retry_reconnects_after_transient_disconnect() -> None:
    class RetryingOperation:
        def __init__(self) -> None:
            self.attempts = 0
            self.reconnections = 0

        def reconnect(self) -> None:
            self.reconnections += 1

        @with_gvfs_retry(attempts=2, initial_delay=0)
        def run(self) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError(errno.ENOTCONN, "mount disconnected")
            return "complete"

    operation = RetryingOperation()
    assert operation.run() == "complete"
    assert operation.attempts == 2
    assert operation.reconnections == 1


def test_public_download_reports_missing_mount_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = MountManager(AppConfig())

    def missing_mount(path: str) -> None:
        raise MountNotFoundError("Drive is not mounted")

    monkeypatch.setattr(manager, "resolve", missing_mount)
    result = download("report.txt", tmp_path / "report.txt", manager=manager)

    assert not result.success
    assert result.error == "Drive is not mounted"
    assert "Download failed; continuing" in capsys.readouterr().err


def test_public_upload_reports_missing_mount_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = MountManager(AppConfig())

    def missing_mount(path: str) -> None:
        raise MountNotFoundError("Drive is not mounted")

    monkeypatch.setattr(manager, "resolve", missing_mount)
    local_path = tmp_path / "report.txt"
    local_path.write_text("test data", encoding="utf-8")

    result = upload(local_path, "report.txt", manager=manager)

    assert not result.success
    assert result.error == "Drive is not mounted"
    assert "Upload failed; continuing" in capsys.readouterr().err


def test_load_file_returns_none_when_mount_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = MountManager(AppConfig())

    def missing_mount(path: str) -> None:
        raise MountNotFoundError("Drive is not mounted")

    monkeypatch.setattr(manager, "resolve", missing_mount)
    local_path = tmp_path / "report.txt"

    result = load_file("report.txt", local_path, manager=manager)

    assert result is None
    assert "Download failed; continuing" in capsys.readouterr().err
