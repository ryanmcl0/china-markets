"""In-process shared state: post queue, deduplication, technical cache, health beats, bot session.

Replaces the Redis-backed version. All state lives in module-level singletons shared
across threads in a single Python process.
"""

from __future__ import annotations

import hashlib
import queue as _stdlib_queue
import threading
import time
from typing import Any

# ── Post queue ──────────────────────────────────────────────────────────────────
_post_queue: _stdlib_queue.Queue = _stdlib_queue.Queue()


def get_queue() -> _stdlib_queue.Queue:
    return _post_queue


# ── Deduplication ───────────────────────────────────────────────────────────────
MAX_DISSEMINATION = 5
_seen: dict[str, int] = {}
_seen_lock = threading.Lock()


def content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            continue
        h.update(p.strip().encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def push_or_boost(post: dict[str, Any]) -> str:
    """Push a new post or boost dissemination on a seen one.

    Returns one of: "new", "boosted", "exhausted".
    """
    chash = post.get("content_hash") or content_hash(
        post.get("source", ""), post.get("url", ""), post.get("text", ""),
    )
    post["content_hash"] = chash

    with _seen_lock:
        count = _seen.get(chash, 0) + 1
        _seen[chash] = count

    if count == 1:
        post["dissemination_count"] = 1
        post["dissemination_boost"] = False
        _post_queue.put(post)
        return "new"
    if count <= MAX_DISSEMINATION:
        post["dissemination_count"] = count
        post["dissemination_boost"] = True
        _post_queue.put(post)
        return "boosted"
    return "exhausted"


# ── Technical cache ─────────────────────────────────────────────────────────────
_tech_cache: dict[str, tuple[dict, float]] = {}
_tech_lock = threading.Lock()


def cache_technical(symbol: str, payload: dict[str, Any], ttl_seconds: int = 1800) -> None:
    with _tech_lock:
        _tech_cache[symbol] = (payload, time.time() + ttl_seconds)


def get_technical(symbol: str) -> dict[str, Any] | None:
    with _tech_lock:
        entry = _tech_cache.get(symbol)
        if entry is None:
            return None
        data, expires_at = entry
        if time.time() > expires_at:
            del _tech_cache[symbol]
            return None
        return data


# ── Health beats ────────────────────────────────────────────────────────────────
_health: dict[str, float] = {}
_health_lock = threading.Lock()


def health_beat(source: str) -> None:
    with _health_lock:
        _health[source] = time.time()


def get_health() -> dict[str, float]:
    with _health_lock:
        return dict(_health)


# ── Bot session + pending-confirmation + pause ──────────────────────────────────
_sessions: dict[str, tuple[list, float]] = {}
_pending: dict[str, tuple[dict, float]] = {}
_pause_until: float = 0.0
_bot_lock = threading.Lock()

SESSION_TTL = 30 * 60
PENDING_TTL = 5 * 60


def get_session(chat_id) -> list[dict]:
    key = str(chat_id)
    with _bot_lock:
        entry = _sessions.get(key)
        if entry is None:
            return []
        msgs, expires_at = entry
        if time.time() > expires_at:
            del _sessions[key]
            return []
        return list(msgs)


def save_session(chat_id, messages: list[dict]) -> None:
    key = str(chat_id)
    with _bot_lock:
        _sessions[key] = (list(messages[-20:]), time.time() + SESSION_TTL)


def save_pending(chat_id, action: dict) -> None:
    key = str(chat_id)
    with _bot_lock:
        _pending[key] = (dict(action), time.time() + PENDING_TTL)


def pop_pending(chat_id) -> dict | None:
    key = str(chat_id)
    with _bot_lock:
        entry = _pending.pop(key, None)
    if entry is None:
        return None
    action, expires_at = entry
    if time.time() > expires_at:
        return None
    return action


def set_pause_until(timestamp: float) -> None:
    global _pause_until
    with _bot_lock:
        _pause_until = float(timestamp)


def get_pause_until() -> float:
    with _bot_lock:
        return _pause_until


def is_paused() -> bool:
    with _bot_lock:
        return _pause_until > time.time()
