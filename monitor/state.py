"""Persistent state between runs.

Stored as JSON under ``state/`` and carried between GitHub Actions runs by
actions/cache (never committed — the repo is public). Losing it is safe:
alerts are additionally bounded by recency, so at worst a recent filing is
re-sent once.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path(os.environ.get("MONITOR_STATE_PATH", "state/state.json"))
PRUNE_DAYS = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _empty() -> dict[str, Any]:
    return {
        "alerted_filings": {},   # filing id -> ISO date alerted
        "last_eval": {},         # ticker -> last bar date evaluated
        "last_signal": {},       # "ticker:kind" -> ISO date
        "tasks": {},             # task name -> last-done marker
        "pause_until": None,     # ISO datetime
        "limit_overrides": {},   # ticker -> limit set via /limit
        "sent": [],              # [{ts, kind, title}] recent outgoing messages
        "last_weekly": None,     # text of the last weekly brief, for continuity
    }


def load() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty()
    base = _empty()
    base.update(data)
    return base


def save(state: dict[str, Any]) -> None:
    _prune(state)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _prune(state: dict[str, Any]) -> None:
    cutoff = (utcnow() - timedelta(days=PRUNE_DAYS)).date().isoformat()
    for key in ("alerted_filings", "last_signal"):
        state[key] = {k: v for k, v in state[key].items() if str(v)[:10] >= cutoff}
    state["sent"] = state["sent"][-50:]


def is_paused(state: dict[str, Any], now: datetime | None = None) -> bool:
    until = state.get("pause_until")
    if not until:
        return False
    return datetime.fromisoformat(until) > (now or utcnow())


def record_sent(state: dict[str, Any], kind: str, title: str) -> None:
    state["sent"].append({"ts": utcnow().isoformat(timespec="minutes"), "kind": kind, "title": title[:120]})
