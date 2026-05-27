"""Safe read/write of config.yaml from the bot.

Every write goes through this module so that:
  1. A timestamped backup is written to /app/config_backups/ first
  2. The file is rewritten atomically (temp file + os.replace)
  3. Comments and formatting are preserved via ruamel.yaml
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config.yaml"))
BACKUP_DIR = Path(os.environ.get("CONFIG_BACKUP_DIR", "/app/config_backups"))

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def _backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUP_DIR / f"config-{ts}.yaml"
    shutil.copy2(CONFIG_PATH, dest)
    logger.info("config backed up to %s", dest)
    return dest


def _load() -> Any:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return _yaml.load(f)


def _save(data: Any) -> None:
    _backup()
    # Atomic replace — write to a temp file in the same dir, then rename.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".config-", suffix=".yaml.tmp", dir=str(CONFIG_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _yaml.dump(data, f)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _find_entry(watchlist: list[Any], ticker: str) -> dict[str, Any] | None:
    for entry in watchlist:
        if str(entry.get("ticker")) == str(ticker):
            return entry
    return None


# ──────────────────────────────────────────────────────────
# Public actions
# ──────────────────────────────────────────────────────────

def update_limit(ticker: str, new_limit: float) -> dict[str, Any]:
    data = _load()
    entry = _find_entry(data.get("watchlist", []), ticker)
    if entry is None:
        raise KeyError(f"ticker {ticker} not in watchlist")
    old = entry.get("gtc_limit")
    entry["gtc_limit"] = new_limit
    _save(data)
    return {"ticker": ticker, "old_limit": old, "new_limit": new_limit}


def record_buy(ticker: str, quantity: float, price: float, currency: str | None = None) -> dict[str, Any]:
    data = _load()
    entry = _find_entry(data.get("watchlist", []), ticker)
    if entry is None:
        raise KeyError(f"ticker {ticker} not in watchlist")

    existing = entry.get("position") or {}
    today = datetime.now(timezone.utc).date().isoformat()
    prev_qty = float(existing.get("quantity") or 0)
    prev_cost = float(existing.get("average_cost") or 0)
    new_qty = prev_qty + float(quantity)
    if new_qty > 0:
        new_avg = ((prev_qty * prev_cost) + (float(quantity) * float(price))) / new_qty
    else:
        new_avg = price

    entry["status"] = "holding"
    entry["position"] = {
        "quantity": round(new_qty, 4),
        "average_cost": round(new_avg, 4),
        "currency": currency or entry.get("currency", "CNY"),
        "date_opened": existing.get("date_opened", today),
        "last_updated": today,
    }
    _save(data)
    return {"ticker": ticker, "position": entry["position"]}


def record_sell(ticker: str, quantity: float | None) -> dict[str, Any]:
    data = _load()
    entry = _find_entry(data.get("watchlist", []), ticker)
    if entry is None:
        raise KeyError(f"ticker {ticker} not in watchlist")
    pos = entry.get("position") or {}
    prev_qty = float(pos.get("quantity") or 0)
    if quantity is None or float(quantity) >= prev_qty:
        entry["status"] = "exited"
        entry.pop("position", None)
        _save(data)
        return {"ticker": ticker, "exited": True}
    pos["quantity"] = round(prev_qty - float(quantity), 4)
    pos["last_updated"] = datetime.now(timezone.utc).date().isoformat()
    entry["position"] = pos
    _save(data)
    return {"ticker": ticker, "exited": False, "remaining": pos["quantity"]}


def add_stock(entry: dict[str, Any]) -> dict[str, Any]:
    data = _load()
    if _find_entry(data.get("watchlist", []), entry["ticker"]):
        raise ValueError(f"ticker {entry['ticker']} already in watchlist")
    data.setdefault("watchlist", []).append(entry)
    tickers = data.setdefault("keywords", {}).setdefault("tickers", [])
    if entry["ticker"] not in tickers:
        tickers.append(entry["ticker"])
    _save(data)
    return {"added": entry["ticker"]}


def remove_stock(ticker: str) -> dict[str, Any]:
    data = _load()
    watchlist = data.get("watchlist", [])
    new_list = [e for e in watchlist if str(e.get("ticker")) != str(ticker)]
    if len(new_list) == len(watchlist):
        raise KeyError(f"ticker {ticker} not in watchlist")
    data["watchlist"] = new_list
    tickers = data.get("keywords", {}).get("tickers", [])
    if ticker in tickers:
        tickers.remove(ticker)
    _save(data)
    return {"removed": ticker}


def add_keyword(category: str, keyword: str) -> dict[str, Any]:
    if category not in {"tickers", "companies_en", "companies_zh", "sector_terms"}:
        raise ValueError(f"unknown keyword category: {category}")
    data = _load()
    bucket = data.setdefault("keywords", {}).setdefault(category, [])
    if keyword in bucket:
        return {"already_present": keyword}
    bucket.append(keyword)
    _save(data)
    return {"added": keyword, "category": category}


def update_weight(ticker: str, new_weight: float) -> dict[str, Any]:
    data = _load()
    entry = _find_entry(data.get("watchlist", []), ticker)
    if entry is None:
        raise KeyError(f"ticker {ticker} not in watchlist")
    old = entry.get("portfolio_weight")
    entry["portfolio_weight"] = new_weight
    _save(data)
    return {"ticker": ticker, "old_weight": old, "new_weight": new_weight}
