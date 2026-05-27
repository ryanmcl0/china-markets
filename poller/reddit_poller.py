"""Reddit poller — pulls watchlist-relevant posts from configured subreddits."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import praw

from post_queue import content_hash, health_beat, push_or_boost

logger = logging.getLogger(__name__)


def _build_client() -> praw.Reddit | None:
    cid = os.environ.get("REDDIT_CLIENT_ID")
    csecret = os.environ.get("REDDIT_CLIENT_SECRET")
    ua = os.environ.get("REDDIT_USER_AGENT", "china-monitor/0.1")
    if not cid or not csecret:
        logger.warning("Reddit credentials not set — Reddit polling disabled")
        return None
    return praw.Reddit(client_id=cid, client_secret=csecret, user_agent=ua)


def _matches(text: str, keywords: list[str]) -> str | None:
    """Return the first keyword found in text, or None."""
    if not text:
        return None
    lowered = text.lower()
    for kw in keywords:
        if kw.lower() in lowered:
            return kw
    return None


def poll_once(config: dict[str, Any]) -> dict[str, int]:
    stats = {"new": 0, "boosted": 0, "exhausted": 0, "errors": 0, "skipped": 0}
    reddit_cfg = config.get("reddit", {})
    if not reddit_cfg.get("enabled"):
        return stats

    client = _build_client()
    if client is None:
        return stats

    kw = config.get("keywords", {})
    all_keywords = (
        kw.get("tickers", [])
        + kw.get("companies_en", [])
        + kw.get("companies_zh", [])
        + kw.get("sector_terms", [])
    )

    for subreddit_name in reddit_cfg.get("subreddits", []):
        clean_name = subreddit_name.removeprefix("r/").strip("/")
        try:
            submissions = list(client.subreddit(clean_name).new(limit=40))
        except Exception as e:
            logger.warning("reddit fetch failed for %s: %s", clean_name, e)
            stats["errors"] += 1
            continue

        for s in submissions:
            full_text = f"{s.title}\n\n{s.selftext or ''}"
            matched = _matches(full_text, all_keywords)
            if not matched:
                stats["skipped"] += 1
                continue

            post = {
                "source": f"reddit/{clean_name}",
                "url": f"https://www.reddit.com{s.permalink}",
                "ticker": None,
                "exchange": None,
                "language": "en",
                "title": s.title,
                "text": full_text,
                "published_at": datetime.fromtimestamp(s.created_utc, tz=timezone.utc).isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "matched_keyword": matched,
                "technical": None,
                "content_hash": content_hash("reddit", s.permalink, full_text),
            }
            result = push_or_boost(post)
            stats[result] = stats.get(result, 0) + 1

    health_beat("reddit")
    logger.info("reddit sweep done: %s", stats)
    return stats
