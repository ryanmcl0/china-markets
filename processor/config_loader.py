"""Read-side config loader for the processor.

The write side lives in ``config_writer.py`` and uses ruamel.yaml to
preserve formatting and comments when the bot updates the file.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config.yaml"))

_cache: dict[str, Any] | None = None
_cache_lock = threading.Lock()
_cache_mtime: float | None = None


def load_config(force: bool = False) -> dict[str, Any]:
    """Load (and cache) the config. Auto-reloads when mtime changes."""
    global _cache, _cache_mtime
    with _cache_lock:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError as e:
            raise FileNotFoundError(f"config.yaml not found at {CONFIG_PATH}") from e

        if force or _cache is None or _cache_mtime != mtime:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                _cache = yaml.safe_load(f)
            _cache_mtime = mtime
            logger.info(
                "config loaded — %d tickers, model=%s",
                len(_cache.get("watchlist", [])),
                _cache.get("llm", {}).get("model"),
            )
        return _cache
