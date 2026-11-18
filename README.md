# gdrive-tool

A Python package for uploading and downloading files through a Google Drive
mount provided by GVFS/GIO on Linux. It uses no Google REST API and has no
mandatory Python runtime dependencies.

## Requirements

- Python 3.11 or later
- Linux with `gio` installed
- A Google Drive account mounted through GNOME/GVFS

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Install development and test tools with:

```bash
python -m pip install -r requirements-dev.txt
pre-commit install
```

## Python API

Drive paths are relative to the mounted account's root. Local paths accept
either `str` or `pathlib.Path`.

```python
from pathlib import Path

from gdrive_tool import download, load_file, upload

upload_result = upload(
    Path.home() / "Documents" / "report.pdf",
    "Projects/report.pdf",
    verify=True,
)

# Replaces the existing Drive file at this path with the new local file.
replacement_result = upload(
    Path.home() / "Documents" / "report-updated.pdf",
    "Projects/report.pdf",
    on_duplicate="overwrite",
    verify=True,
)

download_result = download(
    "Projects/report.pdf",
    Path.home() / "Downloads" / "report.pdf",
    verify=True,
)

# Downloads the file and returns its local path as pathlib.Path.
local_file = load_file("Projects/report.pdf", verify=True)
```

If `remote_path` is omitted from `upload`, the file is placed in the Drive root
with its original local filename. If the local destination is omitted from
`download` or `load_file`, the file is saved in the current directory using its
original filename.

`verify=True` checks the copied file's size and SHA-256 hash. The returned
`TransferResult` includes the number of bytes transferred, the hash, and
whether the operation succeeded, was skipped, or was simulated. The public
`upload` and `download` functions print expected I/O failures to stderr and
return a result with `success=False` and an `error` message instead of raising.
`load_file` returns `None` if the download fails, so callers can keep running:

```python
local_file = load_file("Projects/report.pdf")
if local_file is None:
    print("The file is unavailable; continuing with the next task.")
else:
    process_file(local_file)
```

Unexpected programming errors are not swallowed.

## Command-line interface

```bash
gdrive doctor
gdrive upload ./report.pdf Projects/report.pdf --verify
gdrive upload ./report-updated.pdf Projects/report.pdf --on-duplicate overwrite --verify
gdrive download Projects/report.pdf ./downloaded-report.pdf --verify
```

The default duplicate policy is `skip`. Use `--on-duplicate rename`,
`overwrite`, or `ask` to choose another behavior. `overwrite` replaces the file
at the destination with the uploaded version; the new contents are written
before the destination is replaced. `--dry-run` simulates the operation, and
`--json` emits structured output.

## Transfers and GVFS limitations

- Files are streamed in chunks, with progress displayed by the CLI.
- Transient connection failures are retried with exponential backoff.
- Data is written directly to the destination file. Failed transfers remove
  the incomplete destination when the mount permits.
- SHA-256 is calculated while reading the source. `--verify` also reads the
  destination to validate the completed copy.
- Downloads restore the source modification time on the local filesystem.
  Uploads do not change remote metadata with `chmod` or `utime`.
- The package transfers individual files. It does not copy directories or
  Google-native documents (`.gdoc`, `.gsheet`, `.gslides`, and similar files)
  as regular files.

## Tests

```bash
pytest
```

Tests use a temporary simulated mount and do not require access to a real
Google account.
