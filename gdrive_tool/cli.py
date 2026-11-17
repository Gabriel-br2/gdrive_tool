"""Small command-line interface for Drive file transfers and mount diagnostics."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from .config import configure_logging, load_config
from .exceptions import ExitCode, GDriveError
from .models import TransferProgress, TransferResult
from .mount import MountManager
from .transfer import TransferEngine

LOGGER = logging.getLogger("gdrive_tool.cli")


def _parser() -> argparse.ArgumentParser:
    """Create the deliberately small download/upload/doctor interface."""
    parser = argparse.ArgumentParser(
        prog="gdrive",
        description="Download from and upload to Google Drive through GVFS.",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor_parser = commands.add_parser("doctor", help="check the GVFS Drive mount")
    doctor_parser.add_argument(
        "--remount", action="store_true", help="ask GIO to remount"
    )
    doctor_parser.add_argument("--json", action="store_true", dest="json_command")

    download_parser = commands.add_parser("download", help="download one Drive file")
    download_parser.add_argument("remote_path", help="path relative to the Drive root")
    download_parser.add_argument(
        "local_path",
        nargs="?",
        help="destination file (defaults to the current directory and original name)",
    )
    _add_transfer_options(download_parser)

    upload_parser = commands.add_parser("upload", help="upload one local file")
    upload_parser.add_argument("local_path", help="local file to upload")
    upload_parser.add_argument(
        "remote_path",
        nargs="?",
        help="Drive destination (defaults to the Drive root and original name)",
    )
    _add_transfer_options(upload_parser)
    return parser


def _add_transfer_options(parser: argparse.ArgumentParser) -> None:
    """Add options shared by download and upload."""
    parser.add_argument("--verify", action="store_true", help="verify size and SHA-256")
    parser.add_argument(
        "--dry-run", action="store_true", help="show intent without copying"
    )
    parser.add_argument(
        "--on-duplicate",
        choices=("skip", "rename", "overwrite", "ask"),
        default="skip",
        help="action when a different file already exists (default: skip)",
    )
    parser.add_argument("--json", action="store_true", dest="json_command")


def _print_progress(progress: TransferProgress) -> None:
    """Render a compact ASCII progress indicator on stderr."""
    total = max(progress.total_bytes, 1)
    percent = min(progress.bytes_transferred * 100 // total, 100)
    speed_mib = progress.bytes_per_second / (1024 * 1024)
    eta = "?" if progress.eta_seconds is None else f"{progress.eta_seconds:.0f}s"
    print(
        f"\r{percent:3d}%  {speed_mib:.1f} MiB/s  ETA {eta}",
        end="",
        file=sys.stderr,
        flush=True,
    )


def _show_result(result: TransferResult, *, json_output: bool) -> None:
    """Print a stable transfer result."""
    if json_output:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return
    if result.dry_run:
        print(f"Would transfer: {result.source} -> {result.destination}")
    elif result.skipped:
        print(f"Skipped (already exists): {result.destination}")
    else:
        print(
            f"Transferred {result.bytes_transferred} bytes: "
            f"{result.source} -> {result.destination}"
        )
        if result.sha256:
            print(f"SHA-256: {result.sha256}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and map domain exceptions to stable process exit codes."""
    parser = _parser()
    args = parser.parse_args(argv)
    json_output = bool(args.json or getattr(args, "json_command", False))
    try:
        configure_logging()
        manager = MountManager(load_config())
        if args.command == "doctor":
            report = manager.doctor(remount=args.remount)
            if json_output:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                state = "healthy" if report["healthy"] else "not mounted"
                print(f"Google Drive: {state}")
                for mount in report["mounts"]:
                    print(f"  mount: {mount}")
                if report.get("selection_required"):
                    print("  multiple accounts mounted; configure default_account")
                if report.get("gio_available") is False:
                    print("  gio command is unavailable")
            return int(
                ExitCode.SUCCESS if report["healthy"] else ExitCode.MOUNT_NOT_FOUND
            )

        engine = TransferEngine(manager)
        if args.command == "download":
            local_path = (
                Path(args.local_path)
                if args.local_path
                else Path.cwd() / PurePosixPath(args.remote_path).name
            )
            result = engine.download(
                args.remote_path,
                local_path,
                verify=args.verify,
                dry_run=args.dry_run,
                on_duplicate=args.on_duplicate,
                progress=None if json_output or args.dry_run else _print_progress,
            )
            if not json_output and not result.skipped and not result.dry_run:
                print(file=sys.stderr)
            _show_result(result, json_output=json_output)
            return int(ExitCode.SUCCESS)

        if args.command == "upload":
            local_path = Path(args.local_path)
            remote_path = args.remote_path or local_path.name
            result = engine.upload(
                local_path,
                remote_path,
                verify=args.verify,
                dry_run=args.dry_run,
                on_duplicate=args.on_duplicate,
                progress=None if json_output or args.dry_run else _print_progress,
            )
            if not json_output and not result.skipped and not result.dry_run:
                print(file=sys.stderr)
            _show_result(result, json_output=json_output)
            return int(ExitCode.SUCCESS)
    except GDriveError as error:
        LOGGER.error("%s", error, exc_info=True)
        if json_output:
            print(
                json.dumps(
                    {"error": str(error), "exit_code": int(error.exit_code)},
                    ensure_ascii=False,
                )
            )
        else:
            print(f"gdrive: {error}", file=sys.stderr)
        return int(error.exit_code)
    except (OSError, ValueError) as error:
        if json_output:
            print(
                json.dumps(
                    {"error": str(error), "exit_code": int(ExitCode.GENERAL_ERROR)},
                    ensure_ascii=False,
                )
            )
        else:
            print(f"gdrive: {error}", file=sys.stderr)
        return int(ExitCode.GENERAL_ERROR)
    return int(ExitCode.SUCCESS)


if __name__ == "__main__":
    raise SystemExit(main())
