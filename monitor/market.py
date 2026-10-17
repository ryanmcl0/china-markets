"""Daily prices from Sina (via AKShare) and the metrics derived from them.

Sina is used throughout: Eastmoney's history endpoints drop connections from
datacentre IPs, Sina's don't. Prices are forward-adjusted (qfq), so the latest
close is the real traded price and older bars are adjusted for dividends.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import akshare as ak
import pandas as pd

from .config import Stock

logger = logging.getLogger(__name__)

BENCHMARKS = {"A": "CSI 300", "HK": "Hang Seng"}
HISTORY_START = "20240101"


@dataclass
class Metrics:
    ticker: str
    date: str
    close: float
    prev_close: float
    chg_1d: float
    chg_1w: float
    chg_1m: float
    chg_3m: float
    chg_1y: float
    bench_name: str
    bench_1d: float
    bench_1w: float
    bench_1m: float
    bench_3m: float
    high_52w: float
    low_52w: float
    prev_low_52w: float      # 52-week low *before* today's bar
    rsi14: float | None
    vol_ratio: float | None  # today's volume / 20-day average

    @property
    def rel_1d(self) -> float:
        return self.chg_1d - self.bench_1d

    @property
    def rel_3m(self) -> float:
        return self.chg_3m - self.bench_3m

    @property
    def off_high(self) -> float:
        return (self.close / self.high_52w - 1) * 100

    @property
    def new_52w_low(self) -> bool:
        return self.close < self.prev_low_52w

    def vs_limit(self, limit: float | None) -> float | None:
        return None if not limit else (self.close / limit - 1) * 100

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.update(rel_1d=self.rel_1d, rel_3m=self.rel_3m, off_high=self.off_high)
        return d


# ── Fetching ─────────────────────────────────────────────────────────────────

def _retry(fn, attempts: int = 3):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # akshare raises a zoo of exception types
            if i == attempts - 1:
                raise
            logger.warning("fetch failed (%s), retrying", type(e).__name__)
            time.sleep(3 * (i + 1))


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    for c in ("close", "volume"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def history(stock: Stock) -> pd.DataFrame:
    if stock.is_hk:
        df = _retry(lambda: ak.stock_hk_daily(symbol=stock.ticker, adjust="qfq"))
    else:
        prefix = "sh" if stock.exchange == "XSHG" else "sz"
        df = _retry(lambda: ak.stock_zh_a_daily(symbol=prefix + stock.ticker, start_date=HISTORY_START, adjust="qfq"))
    return _frame(df)


def benchmarks() -> dict[str, pd.Series]:
    csi = _frame(_retry(lambda: ak.stock_zh_index_daily(symbol="sh000300")))
    hsi = _frame(_retry(lambda: ak.stock_hk_index_daily_sina(symbol="HSI")))
    return {"A": csi["close"].dropna(), "HK": hsi["close"].dropna()}


# ── Metrics ──────────────────────────────────────────────────────────────────

def _ret(s: pd.Series, n: int) -> float:
    n = min(n, len(s) - 1)
    if n <= 0:
        return 0.0
    return float((s.iloc[-1] / s.iloc[-1 - n] - 1) * 100)


def rsi(close: pd.Series, period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(close) < period + 1:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    if loss == 0:
        return 100.0
    return round(float(100 - 100 / (1 + gain / loss)), 1)


def compute(ticker: str, bars: pd.DataFrame, bench: pd.Series, bench_name: str) -> Metrics:
    close = bars["close"].dropna()
    year = close[close.index > close.index[-1] - pd.Timedelta(days=365)]
    prior_year = year.iloc[:-1] if len(year) > 1 else year
    # Align the benchmark to the stock's last bar so returns cover the same window.
    b = bench[bench.index <= close.index[-1]]

    vol_ratio = None
    if "volume" in bars and len(bars) > 21:
        vol = bars["volume"].dropna()
        avg = vol.iloc[-21:-1].mean()
        if avg and not math.isnan(avg):
            vol_ratio = round(float(vol.iloc[-1] / avg), 2)

    return Metrics(
        ticker=ticker,
        date=close.index[-1].date().isoformat(),
        close=float(close.iloc[-1]),
        prev_close=float(close.iloc[-2]) if len(close) > 1 else float(close.iloc[-1]),
        chg_1d=_ret(close, 1),
        chg_1w=_ret(close, 5),
        chg_1m=_ret(close, 21),
        chg_3m=_ret(close, 63),
        chg_1y=_ret(close, 245),
        bench_name=bench_name,
        bench_1d=_ret(b, 1),
        bench_1w=_ret(b, 5),
        bench_1m=_ret(b, 21),
        bench_3m=_ret(b, 63),
        high_52w=float(year.max()),
        low_52w=float(year.min()),
        prev_low_52w=float(prior_year.min()),
        rsi14=rsi(close),
        vol_ratio=vol_ratio,
    )


def snapshot(stocks: list[Stock]) -> tuple[dict[str, Metrics], dict[str, Any]]:
    """Metrics for every stock plus a market summary. Failures are skipped, not fatal."""
    bench = benchmarks()
    market = {
        "csi300": {"date": bench["A"].index[-1].date().isoformat(), "1d": _ret(bench["A"], 1),
                   "1w": _ret(bench["A"], 5), "1m": _ret(bench["A"], 21), "3m": _ret(bench["A"], 63)},
        "hsi": {"date": bench["HK"].index[-1].date().isoformat(), "1d": _ret(bench["HK"], 1),
                "1w": _ret(bench["HK"], 5), "1m": _ret(bench["HK"], 21), "3m": _ret(bench["HK"], 63)},
    }
    out: dict[str, Metrics] = {}
    for s in stocks:
        key = "HK" if s.is_hk else "A"
        try:
            out[s.ticker] = compute(s.ticker, history(s), bench[key], BENCHMARKS[key])
        except Exception:
            logger.exception("price fetch failed for %s", s.ticker)
        time.sleep(0.5)
    return out, market
