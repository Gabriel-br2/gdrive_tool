# Copilot instructions

## Build, test, and lint

- Install the package in editable mode: `python -m pip install -e .`
- Install development tools: `python -m pip install -r requirements-dev.txt`
- Build a wheel: `python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .`
- Run the full test suite: `pytest`
- Run one test: `pytest tests/test_transfer.py::test_upload_writes_atomically_and_verifies_hash`
- Lint maintained Python files: `ruff check gdrive_tool tests transfer_example.py setup.py`
- Check formatting: `ruff format --check gdrive_tool tests transfer_example.py setup.py`
- Run all configured hooks: `pre-commit run --all-files`

The project targets Python 3.11+, has no mandatory Python runtime dependencies,
and requires Linux `gio` plus a Google Drive mount through GVFS for real
transfers. Unit tests use temporary directories and do not require a live mount.

## Architecture

- `gdrive_tool.__init__` is the supported convenience API. `download` and
  `upload` return `TransferResult`; expected I/O/domain failures are printed to
  stderr and represented by `success=False` and `error`. `load_file` returns a
  local `Path`, or `None` when the download fails. Unexpected programming
  errors are not suppressed.
- `TransferEngine` in `transfer.py` contains the lower-level transfer workflow
  and raises domain exceptions. It streams one file at a time, writes through
  sibling `.part` files, calculates SHA-256, applies duplicate policies, and
  optionally verifies the completed copy.
- `MountManager` in `mount.py` discovers the active GVFS mount and resolves
  virtual Drive-relative paths to mounted paths and handles mount/reconnect
  commands. Its retry decorator handles transient mount I/O. `transfer.py`
  uses GIO metadata/listing commands and streams file contents through GVFS.
- `cli.py` exposes `doctor`, `download`, and `upload`, formats progress/results,
  and maps `GDriveError` subclasses to process exit codes. `__main__.py` makes
  the same CLI available through `python -m gdrive_tool`.
- `models.py` defines immutable transfer/configuration value objects;
  `exceptions.py` defines domain errors and exit codes; `config.py` owns user
  configuration and structured file logging.

## Repository conventions

- A remote path is a string relative to the selected Drive root (for example,
  `Projects/report.pdf`); a local filesystem path should be a `Path`.
- Access remote files through GVFS/GIO. Use argument lists with `subprocess`,
  never a shell command string. Keep remote metadata operations at the
  directory/file level; do not introduce `os.walk` or per-entry `stat()` loops.
- Preserve streaming and atomic-write behavior. Downloads may restore `mtime`
  locally; uploads must not attempt remote `chmod` or `utime`.
- Google-native documents and empty placeholders are not ordinary byte files.
  Directory transfers are not currently supported.
- Keep operational messages, comments, and documentation in English. Use
  `snake_case` for functions and variables; class names follow Python's
  `CapWords` convention.
