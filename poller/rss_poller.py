"""RSS poller — keyword-filters configured feeds against the watchlist."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import feedparser

from post_queue import content_hash, health_beat, push_or_boost

logger = logging.getLogger(__name__)


def _matches(text: str, keywords: list[str]) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for kw in keywords:
        if kw.lower() in lowered:
            return kw
    return None


def _entry_text(entry: Any) -> str:
    parts = [
        getattr(entry, "title", "") or "",
        getattr(entry, "summary", "") or "",
        getattr(entry, "description", "") or "",
    ]
    return "\n\n".join(p for p in parts if p).strip()


def poll_once(config: dict[str, Any]) -> dict[str, int]:
    stats = {"new": 0, "boosted": 0, "exhausted": 0, "errors": 0, "skipped": 0}
    rss_cfg = config.get("rss", {})
    if not rss_cfg.get("enabled"):
        return stats

    kw = config.get("keywords", {})
    all_keywords = (
        kw.get("tickers", [])
        + kw.get("companies_en", [])
        + kw.get("companies_zh", [])
        + kw.get("sector_terms", [])
    )

    for feed in rss_cfg.get("feeds", []):
        url = feed["url"]
        name = feed.get("name", url)
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            logger.warning("rss fetch failed for %s: %s", name, e)
            stats["errors"] += 1
            continue

        for entry in parsed.entries[:40]:
            text = _entry_text(entry)
            matched = _matches(text, all_keywords)
            if not matched:
                stats["skipped"] += 1
                continue

            link = getattr(entry, "link", "") or ""
            post = {
                "source": f"rss/{name}",
                "url": link,
                "ticker": None,
                "exchange": None,
                "language": "en",
                "title": getattr(entry, "title", "") or "",
                "text": text,
                "published_at": getattr(entry, "published", None) or datetime.now(timezone.utc).isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "matched_keyword": matched,
                "technical": None,
                "content_hash": content_hash("rss", link, text),
            }
            result = push_or_boost(post)
            stats[result] = stats.get(result, 0) + 1

    health_beat("rss")
    logger.info("rss sweep done: %s", stats)
    return stats
