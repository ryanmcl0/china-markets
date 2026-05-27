"""Poller entry point — schedules AKShare, Reddit, and RSS sweeps.

All three pollers share a single Redis connection and read config.yaml
at startup. Intervals come from config — restart the container to pick
up changes.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

import schedule

from config_loader import load_config
from post_queue import get_client

import akshare_poller
import reddit_poller
import rss_poller


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


def _safe_run(name: str, fn) -> None:
    """Wrap a poll cycle in a thread + exception guard so a failing poller
    can never kill the scheduler."""

    def runner():
        try:
            fn()
        except Exception:
            logging.getLogger(name).exception("%s poll cycle failed", name)

    threading.Thread(target=runner, daemon=True, name=f"{name}-poll").start()


def main() -> None:
    _setup_logging()
    log = logging.getLogger("poller")

    config = load_config()
    redis_client = get_client()

    # Ping Redis up-front so we fail loudly if it's not reachable.
    redis_client.ping()
    log.info("connected to Redis")

    akshare_every = config.get("akshare", {}).get("poll_interval_minutes", 20)
    reddit_every = config.get("reddit", {}).get("poll_interval_minutes", 30)
    rss_every = config.get("rss", {}).get("poll_interval_minutes", 30)

    schedule.every(akshare_every).minutes.do(
        _safe_run, "akshare", lambda: akshare_poller.poll_once(redis_client, config)
    )
    if config.get("reddit", {}).get("enabled"):
        schedule.every(reddit_every).minutes.do(
            _safe_run, "reddit", lambda: reddit_poller.poll_once(redis_client, config)
        )
    if config.get("rss", {}).get("enabled"):
        schedule.every(rss_every).minutes.do(
            _safe_run, "rss", lambda: rss_poller.poll_once(redis_client, config)
        )

    # Immediate first sweep so we get data fast rather than waiting one interval.
    _safe_run("akshare", lambda: akshare_poller.poll_once(redis_client, config))
    if config.get("reddit", {}).get("enabled"):
        _safe_run("reddit", lambda: reddit_poller.poll_once(redis_client, config))
    if config.get("rss", {}).get("enabled"):
        _safe_run("rss", lambda: rss_poller.poll_once(redis_client, config))

    log.info(
        "scheduler started: akshare=%dm reddit=%dm rss=%dm",
        akshare_every, reddit_every, rss_every,
    )

    stop = threading.Event()

    def _shutdown(*_):
        log.info("received signal, shutting down")
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop.is_set():
        schedule.run_pending()
        time.sleep(1)

    log.info("poller stopped")
    sys.exit(0)


if __name__ == "__main__":
    main()
