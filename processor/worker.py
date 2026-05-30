"""Processor entry point.

Four threads share this process:
  1. The in-process queue consumer that runs each post through Qwen
  2. The Telegram bot (commands + conversational handler)
  3. The 08:00 digest scheduler
  4. The poller scheduler (AKShare / Reddit / RSS sweeps)
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue as _queue
import signal
import threading
import time
from datetime import datetime

import pytz
import schedule

import akshare_poller
import reddit_poller
import rss_poller
import bot as bot_module
import db
import llm
import notifier
import post_queue as pq
from config_loader import load_config

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


def _is_market_open(config: dict) -> bool:
    """Crude Asia/Shanghai market-hours check used to gate medium-score alerts."""
    sector = config.get("sector", {})
    try:
        tz = pytz.timezone(sector.get("market_timezone", "Asia/Shanghai"))
    except Exception:
        tz = pytz.UTC
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    hours = sector.get("market_hours", "09:30-15:00")
    try:
        start_s, end_s = hours.split("-")
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except Exception:
        return False
    t = now.time()
    return (t.hour, t.minute) >= (sh, sm) and (t.hour, t.minute) < (eh, em)


# ──────────────────────────────────────────────────────────
# Queue consumer
# ──────────────────────────────────────────────────────────

def consumer_loop(stop_event: threading.Event) -> None:
    log = logging.getLogger("worker")
    q = pq.get_queue()
    log.info("worker started, reading from in-process queue")

    while not stop_event.is_set():
        try:
            post = q.get(timeout=5)
        except _queue.Empty:
            continue

        try:
            _process_post(post)
        except Exception:
            log.exception("processing failed for post hash=%s", post.get("content_hash"))


def _process_post(post: dict) -> None:
    log = logging.getLogger("worker")
    config = load_config()
    chash = post.get("content_hash")

    # PRE-CHECK: If we've already analysed this post, just update its dissemination boost.
    existing = db.get_post_by_hash(chash)
    if existing:
        count = post.get("dissemination_count", 1)
        db.update_dissemination(chash, count)
        log.info("updated dissemination boost count=%d hash=%s (skipped LLM)", count, chash)
        return

    analysis = llm.analyse_post(post, config)
    pm = analysis.get("portfolio_manager") or {}
    score = int(pm.get("relevance_score") or 0)
    urgency = (pm.get("urgency") or "low").lower()

    # FORCE PRICE ALERTS: If this is a dedicated price alert, we override
    # the scoring to ensure it bypasses filters and notifies the user.
    is_price_alert = post.get("source") == "price_alert"
    if is_price_alert:
        score = 10
        urgency = "high"

    try:
        post_id = db.insert_post(post, analysis)
    except Exception:
        log.exception("db insert failed; continuing without persistence")
        post_id = None

    threshold = config.get("notifications", {}).get("score_discard", 4)
    # Price alerts bypass discard
    if not is_price_alert and score <= threshold:
        log.info("discard score=%d hash=%s", score, post.get("content_hash"))
        return

    # Price alerts bypass should_notify check (immediate Telegram push)
    if not is_price_alert and not notifier.should_notify(score, urgency, config, market_open=_is_market_open(config)):
        log.info("queued for digest score=%d", score)
        return

    msg_id = notifier.send_alert(post, analysis)
    if post_id is not None:
        try:
            db.mark_notified(post_id, msg_id)
        except Exception:
            log.exception("mark_notified failed")


# ──────────────────────────────────────────────────────────
# Poller scheduler
# ──────────────────────────────────────────────────────────

def _safe_poll(name: str, fn) -> None:
    def runner():
        try:
            fn()
        except Exception:
            logging.getLogger(name).exception("%s poll cycle failed", name)
    threading.Thread(target=runner, daemon=True, name=f"{name}-poll").start()


def poller_loop(stop_event: threading.Event) -> None:
    log = logging.getLogger("poller")
    config = load_config()

    akshare_every = config.get("akshare", {}).get("poll_interval_minutes", 20)
    reddit_every = config.get("reddit", {}).get("poll_interval_minutes", 30)
    rss_every = config.get("rss", {}).get("poll_interval_minutes", 30)

    schedule.every(akshare_every).minutes.do(
        _safe_poll, "akshare", lambda: akshare_poller.poll_once(load_config())
    )
    if config.get("reddit", {}).get("enabled"):
        schedule.every(reddit_every).minutes.do(
            _safe_poll, "reddit", lambda: reddit_poller.poll_once(load_config())
        )
    if config.get("rss", {}).get("enabled"):
        schedule.every(rss_every).minutes.do(
            _safe_poll, "rss", lambda: rss_poller.poll_once(load_config())
        )

    # Immediate first sweep so we get data fast rather than waiting one interval.
    _safe_poll("akshare", lambda: akshare_poller.poll_once(load_config()))
    if config.get("reddit", {}).get("enabled"):
        _safe_poll("reddit", lambda: reddit_poller.poll_once(load_config()))
    if config.get("rss", {}).get("enabled"):
        _safe_poll("rss", lambda: rss_poller.poll_once(load_config()))

    log.info(
        "poller scheduler started: akshare=%dm reddit=%dm rss=%dm",
        akshare_every, reddit_every, rss_every,
    )
    while not stop_event.is_set():
        schedule.run_pending()
        stop_event.wait(1)


# ──────────────────────────────────────────────────────────
# 08:00 digest scheduler
# ──────────────────────────────────────────────────────────

def digest_loop(stop_event: threading.Event) -> None:
    log = logging.getLogger("digest")
    last_sent_date = None
    while not stop_event.is_set():
        try:
            config = load_config()
            n_cfg = config.get("notifications", {})
            tz_name = n_cfg.get("digest_timezone", "Europe/London")
            try:
                tz = pytz.timezone(tz_name)
            except Exception:
                tz = pytz.UTC
            now = datetime.now(tz)
            target_h, target_m = (int(x) for x in n_cfg.get("daily_digest_time", "08:00").split(":"))

            if (now.hour, now.minute) >= (target_h, target_m) and last_sent_date != now.date():
                min_score = n_cfg.get("score_digest_only", 5)
                rows = db.posts_since(hours=24, min_score=min_score)
                _send_digest(rows)
                last_sent_date = now.date()
        except Exception:
            log.exception("digest loop tick failed")
        stop_event.wait(60)


def _send_digest(rows: list[dict]) -> None:
    china_tz = pytz.timezone("Asia/Shanghai")
    now_china = datetime.now(china_tz).strftime("%H:%M")
    
    if not rows:
        notifier.send_message(f"<b>China Energy Market Digest — {now_china} (CN)</b>\n\nNo posts above threshold in the last 24h.")
        return
    
    lines = [f"<b>China Energy Market Digest — {now_china} (CN) | {len(rows)} items</b>", ""]
    for r in rows[:20]:
        tickers = ", ".join(r.get("stocks_mentioned") or []) or "—"
        
        # Use published_at if available, fallback to created_at (which is UTC)
        ts = r.get("published_at") or r.get("created_at")
        china_time = notifier._format_china_time(ts, assume_utc=True)
        time_str = f" {china_time}" if china_time else ""
        
        lines.append(
            f"• [{r['relevance_score']}/10]{time_str} {tickers} — "
            f"{(r.get('action_recommendation') or 'n/a').upper()}"
        )
        summary = r.get("analyst_summary") or ""
        if summary:
            lines.append(f"  <i>{summary[:200]}</i>")
    notifier.send_message("\n".join(lines))


# ──────────────────────────────────────────────────────────
# Bot in its own event loop
# ──────────────────────────────────────────────────────────

def bot_loop(stop_event: threading.Event) -> None:
    log = logging.getLogger("bot")
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        bot_module.run(stop_event)
    except Exception:
        log.exception("bot loop crashed")


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main() -> None:
    _setup_logging()
    log = logging.getLogger("main")

    if not notifier.test_connection():
        log.warning("telegram getMe failed — check TELEGRAM_BOT_TOKEN")
    else:
        log.info("telegram bot reachable")
        notifier.send_message("<b>China Energy Monitor Online</b>\nSystem is scanning 10 tickers via Qwen2.5:3B.")

    stop_event = threading.Event()

    threads = [
        threading.Thread(target=consumer_loop, args=(stop_event,), name="consumer", daemon=True),
        threading.Thread(target=digest_loop, args=(stop_event,), name="digest", daemon=True),
        threading.Thread(target=bot_loop, args=(stop_event,), name="bot", daemon=True),
        threading.Thread(target=poller_loop, args=(stop_event,), name="poller", daemon=True),
    ]
    for t in threads:
        t.start()

    def _shutdown(*_):
        log.info("shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop_event.is_set():
        time.sleep(1)

    log.info("processor stopped")


if __name__ == "__main__":
    main()
