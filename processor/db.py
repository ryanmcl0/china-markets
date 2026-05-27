"""SQLite persistence. Same public API as the Postgres version."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/app/data/china.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    url TEXT,
    url_hash TEXT UNIQUE,
    original_text TEXT,
    translation TEXT,
    analyst_summary TEXT,
    analyst_credibility TEXT,
    analyst_is_new_info INTEGER,
    stocks_mentioned TEXT,
    sentiment TEXT,
    category TEXT,
    dissemination_count INTEGER DEFAULT 1,
    dissemination_signal TEXT,
    rsi REAL,
    rsi_signal TEXT,
    price_vs_ma20 TEXT,
    ma20_pct REAL,
    current_price REAL,
    divergence TEXT,
    divergence_note TEXT,
    relevance_score INTEGER,
    urgency TEXT,
    notified INTEGER DEFAULT 0,
    action_recommendation TEXT,
    action_reasoning TEXT,
    action_confidence TEXT,
    action_time_horizon TEXT,
    action_conditions TEXT,
    rules_checked TEXT,
    processed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER REFERENCES posts(id),
    telegram_message_id INTEGER,
    sent_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS actions_taken (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER,
    ticker TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    notes TEXT,
    taken_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_score ON posts(relevance_score);
"""


def _ensure_schema() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_post_by_hash(url_hash: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM posts WHERE url_hash = ?", (url_hash,)).fetchone()
        return dict(row) if row else None


def update_dissemination(url_hash: str, count: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE posts SET dissemination_count = ?, processed_at = ? WHERE url_hash = ?",
            (count, datetime.now(timezone.utc).isoformat(), url_hash),
        )


def insert_post(post: dict[str, Any], llm_out: dict[str, Any]) -> int:
    analyst = llm_out.get("analyst") or {}
    technical = llm_out.get("technical") or {}
    pm = llm_out.get("portfolio_manager") or {}
    tech_ctx = post.get("technical") or {}

    stocks = analyst.get("stocks_mentioned") or []
    rules = pm.get("rules_checked")

    params = (
        post.get("source"),
        post.get("url"),
        post.get("content_hash"),
        post.get("text"),
        llm_out.get("translation"),
        analyst.get("summary"),
        analyst.get("credibility"),
        1 if analyst.get("is_new_information") else 0,
        json.dumps(stocks),
        analyst.get("sentiment"),
        analyst.get("category"),
        post.get("dissemination_count", 1),
        analyst.get("dissemination_signal"),
        tech_ctx.get("rsi"),
        tech_ctx.get("rsi_signal"),
        tech_ctx.get("vs_ma20"),
        tech_ctx.get("ma20_pct"),
        tech_ctx.get("current_price"),
        technical.get("divergence"),
        technical.get("divergence_note"),
        pm.get("relevance_score"),
        pm.get("urgency"),
        pm.get("recommendation"),
        pm.get("reasoning"),
        pm.get("confidence"),
        pm.get("time_horizon"),
        pm.get("conditions"),
        json.dumps(rules) if isinstance(rules, list) else rules,
        datetime.now(timezone.utc).isoformat(),
    )
    sql = """
        INSERT INTO posts (
            source, url, url_hash, original_text, translation,
            analyst_summary, analyst_credibility, analyst_is_new_info,
            stocks_mentioned, sentiment, category,
            dissemination_count, dissemination_signal,
            rsi, rsi_signal, price_vs_ma20, ma20_pct, current_price,
            divergence, divergence_note,
            relevance_score, urgency,
            action_recommendation, action_reasoning, action_confidence,
            action_time_horizon, action_conditions, rules_checked,
            processed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with connect() as conn:
        try:
            conn.execute(sql, params)
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE posts SET dissemination_count=?, processed_at=? WHERE url_hash=?",
                (post.get("dissemination_count", 1), datetime.now(timezone.utc).isoformat(), post.get("content_hash")),
            )
            row = conn.execute("SELECT id FROM posts WHERE url_hash=?", (post.get("content_hash"),)).fetchone()
            return row[0] if row else 0


def mark_notified(post_id: int, telegram_message_id: int | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE posts SET notified=1 WHERE id=?", (post_id,))
        conn.execute(
            "INSERT INTO notifications (post_id, telegram_message_id) VALUES (?,?)",
            (post_id, telegram_message_id),
        )


def record_action(ticker: str, action: str, notes: str | None, post_id: int | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO actions_taken (post_id, ticker, action_taken, notes) VALUES (?,?,?,?)",
            (post_id, ticker, action, notes),
        )


def posts_since(hours: int, min_score: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    sql = """
        SELECT id, source, url, original_text, translation, analyst_summary,
               sentiment, category, stocks_mentioned, relevance_score, urgency,
               action_recommendation, action_reasoning, created_at
        FROM posts
        WHERE created_at > ? AND relevance_score >= ?
        ORDER BY relevance_score DESC, created_at DESC
    """
    with connect() as conn:
        rows = conn.execute(sql, (cutoff, min_score)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            raw = d.get("stocks_mentioned")
            if raw:
                try:
                    d["stocks_mentioned"] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d["stocks_mentioned"] = []
            result.append(d)
        return result


def post_count_today() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM posts WHERE date(created_at) = date('now')").fetchone()
        return row[0] if row else 0


def last_signal_per_ticker(tickers: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Most-recent post per ticker for /watchlist output."""
    out: dict[str, dict[str, Any]] = {}
    sql = """
        SELECT id, relevance_score, action_recommendation, created_at
        FROM posts
        WHERE instr(stocks_mentioned, ?) > 0
        ORDER BY created_at DESC
        LIMIT 1
    """
    with connect() as conn:
        for t in tickers:
            row = conn.execute(sql, (f'"{t}"',)).fetchone()
            if row:
                out[t] = dict(row)
    return out


# Ensure schema exists when module is first imported
_ensure_schema()
