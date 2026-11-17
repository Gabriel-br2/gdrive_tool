"""Configuration and structured logging support."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError
from .models import AppConfig

APP_DIRECTORY = Path.home() / ".config" / "gdrive-tool"
CONFIG_PATH = APP_DIRECTORY / "config.json"
LOG_PATH = APP_DIRECTORY / "gdrive.log"


def _config_from_payload(payload: dict[str, Any]) -> AppConfig:
    """Validate persisted JSON and convert it to an immutable config."""
    try:
        ttl = payload.get("cache_ttl_seconds", 600)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
            raise ValueError("cache_ttl_seconds must be a non-negative integer")
        account = payload.get("default_account")
        if account is not None and not isinstance(account, str):
            raise ValueError("default_account must be a string or null")
        favorites = payload.get("favorites", [])
        recents = payload.get("recents", [])
        shortcuts = payload.get("shortcuts", {})
        if not isinstance(favorites, list) or not all(
            isinstance(item, str) for item in favorites
        ):
            raise ValueError("favorites must be a list of strings")
        if not isinstance(recents, list) or not all(
            isinstance(item, str) for item in recents
        ):
            raise ValueError("recents must be a list of strings")
        if not isinstance(shortcuts, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in shortcuts.items()
        ):
            raise ValueError("shortcuts must map strings to strings")
        return AppConfig(
            default_account=account,
            cache_ttl_seconds=ttl,
            favorites=tuple(favorites),
            recents=tuple(recents),
            shortcuts=tuple(sorted(shortcuts.items())),
        )
    except (TypeError, ValueError) as error:
        raise ConfigurationError(
            f"Invalid configuration in {CONFIG_PATH}: {error}"
        ) from error


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Load configuration, returning defaults when no file exists."""
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except FileNotFoundError:
        return AppConfig()
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"Cannot read configuration {path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ConfigurationError(f"Configuration {path} must contain a JSON object")
    return _config_from_payload(payload)


def save_config(config: AppConfig, path: Path = CONFIG_PATH) -> None:
    """Atomically persist configuration while restricting its permissions."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.tmp")
        payload = asdict(config)
        payload["shortcuts"] = dict(config.shortcuts)
        with temporary_path.open("w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2)
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        temporary_path.replace(path)
        path.chmod(0o600)
    except OSError as error:
        raise ConfigurationError(
            f"Cannot save configuration {path}: {error}"
        ) from error


class _JsonLogFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(path: Path = LOG_PATH) -> logging.Logger:
    """Configure the package's rotating-independent structured file log."""
    logger = logging.getLogger("gdrive_tool")
    if logger.handlers:
        return logger
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(
            f"Cannot initialize log file {path}: {error}"
        ) from error
    handler.setFormatter(_JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
