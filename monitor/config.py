"""Load config.yaml and normalise watchlist entries.

In GitHub Actions the file is written from the MONITOR_CONFIG secret at the
start of each run; locally it's ./config.yaml (gitignored).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(os.environ.get("MONITOR_CONFIG_PATH", "config.yaml"))

CURRENCY_SYMBOL = {"CNY": "¥", "HKD": "HK$", "USD": "$"}


@dataclass
class Stock:
    ticker: str
    exchange: str            # XSHE / XSHG / HKEX
    name_en: str
    short_en: str            # ≤9 chars, for the phone-width scoreboard
    short_zh: str            # exchange short name, used for news search
    currency: str
    limit: float | None
    tier: str                # core / satellite / speculative
    status: str
    thesis: str
    filings_code: str | None  # A-share code for CNINFO filings (HK names: their A-share twin)
    tags: list[str] = field(default_factory=list)

    @property
    def is_hk(self) -> bool:
        return self.exchange == "HKEX"

    @property
    def symbol(self) -> str:
        return CURRENCY_SYMBOL.get(self.currency, "")

    def price(self, value: float | None) -> str:
        if value is None:
            return "—"
        return f"{self.symbol}{value:,.2f}"


def load(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _tier(tags: list[str]) -> str:
    for t in ("core", "speculative"):
        if t in tags:
            return t
    return "satellite"


def stocks(config: dict[str, Any], limit_overrides: dict[str, float] | None = None) -> list[Stock]:
    overrides = limit_overrides or {}
    out = []
    for e in config.get("watchlist", []):
        ticker = str(e["ticker"])
        exchange = e.get("exchange", "XSHE")
        tags = list(e.get("tags") or [])
        limit = overrides.get(ticker, e.get("gtc_limit"))
        out.append(
            Stock(
                ticker=ticker,
                exchange=exchange,
                name_en=e.get("name_en") or ticker,
                short_en=str(e.get("short_en") or e.get("name_en") or ticker)[:9],
                short_zh=e.get("short_zh") or e.get("name_zh") or ticker,
                currency=e.get("currency") or ("HKD" if exchange == "HKEX" else "CNY"),
                limit=float(limit) if limit is not None else None,
                tier=_tier(tags),
                status=e.get("status", "watching"),
                thesis=" ".join(str(e.get("thesis") or "").split()),
                filings_code=str(e["filings_code"]) if e.get("filings_code") else (None if exchange == "HKEX" else ticker),
                tags=tags,
            )
        )
    return out


def find(stocks_: list[Stock], query: str) -> Stock | None:
    q = query.strip().lower()
    if not q:
        return None
    for s in stocks_:
        if q in (s.ticker.lower(), s.ticker.lstrip("0").lower()):
            return s
    for s in stocks_:
        if q in s.name_en.lower() or q in s.short_zh:
            return s
    return None
