"""Price signals worth a message, evaluated once per new daily bar.

Every signal carries an attribution — was the move the whole market, the
sector, or this stock alone? That is the question the investment approach
turns on: a market- or sector-wide dip with no bad company news is the entry
point the strategy is waiting for; a stock-specific drop needs a reason first.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date

from .config import Stock
from .market import Metrics

MOVE_PCT = 5.0          # |1-day move| that's worth a message
SPIKE_PCT = 9.5         # limit-up territory on the main boards
VOLUME_RATIO = 3.0      # volume vs 20-day average…
VOLUME_MOVE_PCT = 3.0   # …together with at least this move
ATTRIBUTION_BAND = 2.0  # percentage points
NEW_LOW_COOLDOWN_DAYS = 14
MIN_PEERS = 3


@dataclass
class Signal:
    stock: Stock
    metrics: Metrics
    kinds: list[str] = field(default_factory=list)
    attribution: str = ""
    peer_median_1d: float = 0.0

    def describe(self) -> list[str]:
        m, s = self.metrics, self.stock
        lines = []
        if "limit_cross" in self.kinds:
            lines.append(f"Crossed below your {s.price(s.limit)} limit (prev close {s.price(m.prev_close)})")
        if "spike" in self.kinds:
            lines.append(f"Spike of {m.chg_1d:+.1f}% — your rules say don't chase")
        elif "big_move" in self.kinds:
            lines.append(f"Moved {m.chg_1d:+.1f}% on the day")
        if "new_low" in self.kinds:
            lines.append(f"New 52-week low (previous low {s.price(m.prev_low_52w)})")
        if "volume" in self.kinds:
            lines.append(f"Volume {m.vol_ratio:.1f}× its 20-day average")
        return lines


def attribute(m: Metrics, peer_median_1d: float) -> str:
    """Classify a day's move as market-wide, sector-wide or stock-specific."""
    if abs(m.rel_1d) < ATTRIBUTION_BAND:
        return "market-wide"
    if abs(peer_median_1d) >= ATTRIBUTION_BAND and abs(m.chg_1d - peer_median_1d) < ATTRIBUTION_BAND:
        return "sector-wide"
    return "stock-specific"


def evaluate(
    stocks: list[Stock],
    metrics: dict[str, Metrics],
    last_eval: dict[str, str],
    last_signal: dict[str, str],
) -> list[Signal]:
    """Signals for bars not evaluated before. Updates ``last_eval``/``last_signal`` in place."""
    out = []
    for s in stocks:
        m = metrics.get(s.ticker)
        if m is None or last_eval.get(s.ticker) == m.date:
            continue
        last_eval[s.ticker] = m.date

        kinds = []
        if s.limit and m.close <= s.limit < m.prev_close:
            kinds.append("limit_cross")
        if m.chg_1d >= SPIKE_PCT:
            kinds.append("spike")
        elif abs(m.chg_1d) >= MOVE_PCT:
            kinds.append("big_move")
        if m.new_52w_low:
            key = f"{s.ticker}:new_low"
            last = last_signal.get(key)
            if not last or _days_between(last, m.date) >= NEW_LOW_COOLDOWN_DAYS:
                kinds.append("new_low")
                last_signal[key] = m.date
        if m.vol_ratio and m.vol_ratio >= VOLUME_RATIO and abs(m.chg_1d) >= VOLUME_MOVE_PCT:
            kinds.append("volume")

        if kinds:
            peer_median = _peer_median(s.ticker, metrics)
            out.append(Signal(s, m, kinds, attribute(m, peer_median), peer_median))
    return out


def _peer_median(ticker: str, metrics: dict[str, Metrics]) -> float:
    """Median day move of the *other* watchlist stocks (0 if too few to mean anything)."""
    peers = [m.chg_1d for t, m in metrics.items() if t != ticker]
    return statistics.median(peers) if len(peers) >= MIN_PEERS else 0.0


def _days_between(a: str, b: str) -> int:
    return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)
