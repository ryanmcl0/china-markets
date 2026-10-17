"""Entry point.

  python -m monitor plan              decide what's due (stdlib only; prints GitHub step outputs)
  python -m monitor run TASK [TASK…]  run tasks: close, filings, weekly, sector, inbox
  python -m monitor serve             loop plan+run every 15 minutes (NAS / any always-on box)
  python -m monitor context TASK      print the data bundle Claude would get (debugging)

--dry-run prints messages instead of sending them. Never use it in Actions:
the repo is public and so are its logs.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from . import schedule, state as st, telegram

TASK_ORDER = ["inbox", "close", "filings", "weekly", "sector"]

log = logging.getLogger("monitor")


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # AKShare's progress bars and request logs add noise, nothing else.
    for noisy in ("urllib3", "httpx", "akshare"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def plan(state: dict, now: datetime | None = None, peek: bool = True) -> tuple[list[str], bool]:
    now = now or datetime.now(timezone.utc)
    tasks = schedule.due_tasks(now, state["tasks"])
    forced = os.environ.get("FORCE_TASK", "").strip()
    if forced:
        tasks += [t for t in forced.split(",") if t and t not in tasks]
    if peek and telegram.pending_updates():
        tasks.append("inbox")
    keepalive = schedule.keepalive_due(now, state["tasks"])
    if keepalive:
        state["tasks"]["keepalive"] = now.isoformat(timespec="minutes")
    return sorted(set(tasks), key=TASK_ORDER.index), keepalive


def run(tasks: list[str], dry_run: bool = False) -> int:
    # Heavy imports only here, so `plan` works before dependencies are installed.
    from . import config as cfg
    from . import jobs

    state = st.load()
    sess = jobs.Session(cfg.load(), state, dry_run=dry_run)
    runners = {"close": jobs.close, "filings": jobs.filings_check, "weekly": jobs.weekly, "sector": jobs.sector,
               "inbox": jobs.inbox}
    failures = 0
    for task in sorted(set(tasks), key=TASK_ORDER.index):
        log.info("task %s: start", task)
        try:
            runners[task](sess)
            log.info("task %s: done", task)
        except Exception:
            failures += 1
            log.exception("task %s failed", task)
        st.save(state)  # after each task, so one failure doesn't lose the others' progress
    return 1 if failures else 0


def serve(every_minutes: int) -> None:
    log.info("serving: checking every %d minutes", every_minutes)
    while True:
        state = st.load()
        tasks, _ = plan(state)
        st.save(state)
        if tasks:
            run(tasks)
        time.sleep(every_minutes * 60)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    p = argparse.ArgumentParser(prog="monitor")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    r = sub.add_parser("run")
    r.add_argument("tasks", nargs="*", help="defaults to $MONITOR_TASKS")
    r.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("serve")
    s.add_argument("--every", type=int, default=15)
    c = sub.add_parser("context")
    c.add_argument("task", choices=["weekly"])
    args = p.parse_args(argv)

    if args.cmd == "plan":
        state = st.load()
        tasks, keepalive = plan(state)
        if keepalive:
            st.save(state)
        # GitHub step outputs (also readable by a human).
        print(f"tasks={','.join(tasks)}")
        print(f"work={'true' if tasks else 'false'}")
        print(f"keepalive={'true' if keepalive else 'false'}")
        return 0

    if args.cmd == "run":
        tasks = args.tasks or [t for t in os.environ.get("MONITOR_TASKS", "").split(",") if t]
        unknown = set(tasks) - set(TASK_ORDER)
        if unknown:
            p.error(f"unknown task(s): {', '.join(sorted(unknown))}")
        return run(tasks, dry_run=args.dry_run)

    if args.cmd == "serve":
        serve(args.every)
        return 0

    if args.cmd == "context":
        from . import config as cfg, filings, jobs, news, report

        sess = jobs.Session(cfg.load(), st.load(), dry_run=True)
        metrics, mkt = sess.snapshot()
        recent = [f for f in filings.fetch_all(sess.filing_codes, days=7) if f.tier > 0]
        headlines = {s.ticker: news.company(s.short_zh, max_age_days=7) for s in sess.stocks}
        themes = {t["name"]: news.theme(t.get("queries", []), t.get("title_terms", []), 14)
                  for t in sess.config.get("themes", [])}
        print(report.context(sess.config, sess.stocks, metrics, mkt, filings=recent,
                             news_by_ticker=headlines, theme_news=themes))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
