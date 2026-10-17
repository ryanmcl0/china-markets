"""Decide which tasks are due on this run.

The workflow fires every 15 minutes; GitHub's cron is best-effort and runs are
sometimes skipped or late, so tasks are "due once per period, any time after
their start" rather than pinned to an exact minute. All times are UTC.

  close    weekdays from 08:45 (after the HK close at 08:00 UTC) — once per day
  filings  every 2 hours, 08:00–21:59 — CNINFO publishes mostly 08:00–14:00 UTC
  weekly   Saturdays from 08:00 — once per ISO week (the watchlist brief)
  sector   Sundays from 08:00 — once per ISO week (the whole China nuclear/renewables market)
  inbox    whenever Telegram has unread messages for us
"""

from __future__ import annotations

from datetime import datetime, timedelta

CLOSE_AFTER = (8, 45)
FILINGS_WINDOW = (8, 22)
FILINGS_EVERY = timedelta(hours=2)
WEEKLY_AFTER = (8, 0)
KEEPALIVE_EVERY = timedelta(days=20)


def _at_or_after(now: datetime, hm: tuple[int, int]) -> bool:
    return (now.hour, now.minute) >= hm


def week_key(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def due_tasks(now: datetime, tasks_done: dict[str, str]) -> list[str]:
    due: list[str] = []
    today = now.date().isoformat()

    if now.weekday() < 5 and _at_or_after(now, CLOSE_AFTER) and tasks_done.get("close") != today:
        due.append("close")

    if FILINGS_WINDOW[0] <= now.hour < FILINGS_WINDOW[1]:
        last = tasks_done.get("filings")
        if not last or now - datetime.fromisoformat(last) >= FILINGS_EVERY:
            due.append("filings")

    if now.weekday() == 5 and _at_or_after(now, WEEKLY_AFTER) and tasks_done.get("weekly") != week_key(now):
        due.append("weekly")

    if now.weekday() == 6 and _at_or_after(now, WEEKLY_AFTER) and tasks_done.get("sector") != week_key(now):
        due.append("sector")

    return due


def keepalive_due(now: datetime, tasks_done: dict[str, str]) -> bool:
    """Public repos have schedules disabled after 60 days without activity."""
    last = tasks_done.get("keepalive")
    return not last or now - datetime.fromisoformat(last) >= KEEPALIVE_EVERY
