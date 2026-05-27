"""AKShare-backed news + price poller.

Pulls per-ticker news from Eastmoney (A-shares and HK),
calculates RSI and MA20 from daily history, and emits
posts into the queue with technical context cached for 30 minutes.
"""

from __future__ import annotations

import logging
import math
import random
import time
from datetime import datetime, timezone
from typing import Any

import akshare as ak
import pandas as pd

from post_queue import (
    cache_technical,
    content_hash,
    get_technical,
    health_beat,
    push_or_boost,
)

logger = logging.getLogger(__name__)

# List of real browser User-Agents to rotate
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

# Global cooldown state to avoid "hammering" after a block
_GLOBAL_COOLDOWN_UNTIL = 0

def _is_cooling_down() -> bool:
    return time.time() < _GLOBAL_COOLDOWN_UNTIL

def _trigger_cooldown(minutes: int = 15):
    global _GLOBAL_COOLDOWN_UNTIL
    _GLOBAL_COOLDOWN_UNTIL = time.time() + (minutes * 60)
    logger.warning("anti-bot triggered — global news cooldown for %d minutes", minutes)

# ──────────────────────────────────────────────────────────
# Technical context — RSI + MA20
# ──────────────────────────────────────────────────────────

def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    last_loss = loss.iloc[-1]
    last_gain = gain.iloc[-1]
    if last_loss is None or math.isnan(last_loss) or last_loss == 0:
        return 100.0 if last_gain and last_gain > 0 else 50.0
    rs = last_gain / last_loss
    return round(100 - (100 / (1 + rs)), 1)


def get_technical_context(symbol: str, exchange: str) -> dict[str, Any] | None:
    """Fetch history and compute RSI + MA20 features."""
    # We do NOT check global cooldown here anymore, as that's for news.
    
    close = pd.Series()
    max_attempts = 2 # Reduced attempts to be less aggressive
    
    for attempt in range(max_attempts):
        try:
            if exchange in ("XSHE", "XSHG"):
                # A-share history (Eastmoney)
                hist = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date="20250101", adjust="qfq")
            else:
                # HK history (Eastmoney)
                hist = ak.stock_hk_hist(symbol=symbol, period="daily", start_date="20250101", adjust="qfq")
            
            if hist is not None and not hist.empty:
                col = next((c for c in ["收盘", "close", "Close"] if c in hist.columns), None)
                if col:
                    close = pd.to_numeric(hist[col], errors="coerce").dropna()
                    if not close.empty:
                        break
            
            time.sleep(random.uniform(2.0, 5.0))

        except Exception as e:
            err_msg = str(e).lower()
            if "connection aborted" in err_msg or "remotedisconnected" in err_msg:
                if attempt < max_attempts - 1:
                    wait = 10 + random.uniform(5, 10)
                    logger.debug("tech aborted for %s, retrying in %ds...", symbol, int(wait))
                    time.sleep(wait)
                    continue
                else:
                    # We failed technical data, but we DON'T trigger a global cooldown.
                    # Just return None so we can proceed with news.
                    return None
            
            if attempt == max_attempts - 1:
                logger.debug("technical fetch failed for %s (%s): %s", symbol, exchange, e)
                return None
            time.sleep(random.uniform(2.0, 5.0))

    if close.empty or len(close) < 20:
        return None

    rsi = _rsi(close)
    if rsi is None:
        return None

    ma20 = close.rolling(20).mean().iloc[-1]
    current = float(close.iloc[-1])
    if math.isnan(ma20):
        return None
    ma20 = float(ma20)
    ma20_pct = round(((current - ma20) / ma20) * 100, 1)

    return {
        "rsi": rsi,
        "rsi_signal": "oversold" if rsi < 35 else "overbought" if rsi > 70 else "neutral",
        "vs_ma20": "above" if current > ma20 else "below",
        "ma20_pct": ma20_pct,
        "current_price": round(current, 4),
        "fetched_at": int(time.time()),
    }


def fetch_or_cache_technical(symbol: str, exchange: str) -> dict[str, Any] | None:
    cached = get_technical(symbol)
    if cached:
        return cached
    # Note: We don't cache failures, we'll try again next poll.
    return get_technical_context(symbol, exchange)


# ──────────────────────────────────────────────────────────
# News fetching
# ──────────────────────────────────────────────────────────

def _row_to_post(row: pd.Series, source: str, ticker_info: dict[str, Any], technical: dict[str, Any] | None) -> dict[str, Any]:
    title = str(row.get("新闻标题") or row.get("title") or "").strip()
    content = str(row.get("新闻内容") or row.get("content") or row.get("摘要") or "").strip()
    url = str(row.get("新闻链接") or row.get("url") or "").strip()
    published = str(row.get("发布时间") or row.get("publish_time") or row.get("date") or "").strip()
    text = f"{title}\n\n{content}".strip() if content else title

    return {
        "source": source,
        "url": url or None,
        "ticker": ticker_info["ticker"],
        "exchange": ticker_info["exchange"],
        "name_en": ticker_info.get("name_en"),
        "name_zh": ticker_info.get("name_zh"),
        "language": "zh",
        "title": title,
        "text": text,
        "published_at": published or None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "technical": technical,
        "content_hash": content_hash(source, url, text),
    }


def fetch_news(symbol: str, limit: int) -> pd.DataFrame:
    """Consolidated news fetch using Eastmoney (supports A + HK)."""
    if _is_cooling_down():
        return pd.DataFrame()
        
    try:
        df = ak.stock_news_em(symbol=symbol)
        if df is None or df.empty:
            return pd.DataFrame()
        return df.head(limit)
    except Exception as e:
        err_msg = str(e).lower()
        if "connection aborted" in err_msg or "remotedisconnected" in err_msg:
             # ONLY news failures trigger the big cooldown.
             _trigger_cooldown(15)
        logger.warning("news fetch failed for %s: %s", symbol, e)
        return pd.DataFrame()


# ──────────────────────────────────────────────────────────
# Top-level poll loop
# ──────────────────────────────────────────────────────────

def poll_once(config: dict[str, Any]) -> dict[str, int]:
    """Single sweep across the watchlist. Returns per-source counters."""
    stats = {"new": 0, "boosted": 0, "exhausted": 0, "errors": 0}
    
    if _is_cooling_down():
        logger.info("sweep skipped — cooling down from anti-bot trigger")
        return stats

    limit = config.get("akshare", {}).get("news_limit", 20)
    include_hk = config.get("akshare", {}).get("include_hk", True)
    watchlist = config.get("watchlist", [])
    
    # Shuffle watchlist to avoid predictable patterns
    watchlist_shuffled = list(watchlist)
    random.shuffle(watchlist_shuffled)

    consecutive_tech_failures = 0
    skip_tech_this_sweep = False

    for entry in watchlist_shuffled:
        if _is_cooling_down():
            break
            
        ticker = entry["ticker"]
        exchange = entry["exchange"]
        if exchange == "HKEX" and not include_hk:
            continue

        # Significant jitter between stocks (5-12s)
        time.sleep(random.uniform(5.0, 12.0))
        
        technical = None
        if not skip_tech_this_sweep:
            try:
                technical = fetch_or_cache_technical(ticker, exchange)
                if technical:
                    # Success! Cache it.
                    cache_technical(ticker, technical)
                    consecutive_tech_failures = 0
                else:
                    consecutive_tech_failures += 1
            except Exception:
                consecutive_tech_failures += 1
                logger.debug("technical context failed for %s", ticker)

        if consecutive_tech_failures >= 3:
            logger.warning("high tech-fetch failure rate; skipping price data for the rest of this sweep")
            skip_tech_this_sweep = True
        
        # Another small delay between tech and news fetch
        time.sleep(random.uniform(1.0, 3.0))
        
        # News fetch is the primary mission.
        df = fetch_news(ticker, limit)

        if df.empty:
            continue

        for _, row in df.iterrows():
            post = _row_to_post(row, "eastmoney", entry, technical)
            if not post["text"]:
                continue
            result = push_or_boost(post)
            stats[result] = stats.get(result, 0) + 1

    health_beat("akshare")
    logger.info("akshare sweep done: %s", stats)
    return stats
