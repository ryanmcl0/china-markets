"""Telegram message formatting + send."""

from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from typing import Any

import pytz
import requests

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
BASE_URL = "https://api.telegram.org/bot{token}/{method}"


# ──────────────────────────────────────────────────────────
# Send helpers
# ──────────────────────────────────────────────────────────

def _post(method: str, payload: dict[str, Any], timeout: int = 10) -> dict[str, Any] | None:
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not configured")
        return None
    url = BASE_URL.format(token=BOT_TOKEN, method=method)
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.error("telegram %s failed: %s", method, e)
        return None


def send_message(text: str, chat_id: str | None = None) -> int | None:
    """Send to all configured chats (or one explicit chat_id). Returns the
    first message_id sent, or None on total failure."""
    targets = [chat_id] if chat_id else CHAT_IDS
    if not targets:
        logger.error("no chat_ids configured")
        return None

    first_id: int | None = None
    for cid in targets:
        logger.info("attempting push notification to chat_id=%s", cid)
        payload = {
            "chat_id": cid,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = _post("sendMessage", payload)
        if resp and resp.get("ok"):
            mid = resp["result"].get("message_id")
            logger.info("push success to chat_id=%s | message_id=%s", cid, mid)
            if first_id is None:
                first_id = mid
        else:
            logger.error("push failed to chat_id=%s: %s", cid, resp)
    return first_id


# ──────────────────────────────────────────────────────────
# Quiet hours
# ──────────────────────────────────────────────────────────

def within_quiet_hours(config: dict[str, Any]) -> bool:
    cfg = config.get("notifications", {}).get("quiet_hours", {})
    if not cfg.get("enabled"):
        return False
    tz_name = cfg.get("timezone", "UTC")
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.UTC
    now = datetime.now(tz).time()
    start = datetime.strptime(cfg.get("start", "23:00"), "%H:%M").time()
    end = datetime.strptime(cfg.get("end", "07:00"), "%H:%M").time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def should_notify(score: int, urgency: str, config: dict[str, Any], market_open: bool = False) -> bool:
    thresholds = config.get("notifications", {})
    if score >= thresholds.get("score_immediate", 9):
        return True
    if within_quiet_hours(config):
        return False
    if score >= thresholds.get("score_market_hours", 7):
        return market_open or urgency == "high"
    return False


# ──────────────────────────────────────────────────────────
# Formatting
# ──────────────────────────────────────────────────────────

_SENTIMENT_EMOJI = {
    "bullish": "🟢",
    "bearish": "🔴",
    "neutral": "🟡",
}

_ACTION_LABEL = {
    "buy": "🟢 BUY",
    "add": "🟢 ADD TO POSITION",
    "hold": "🟡 HOLD",
    "wait_for_dip": "🟡 WAIT FOR DIP",
    "watch": "👀 WATCH",
    "reduce": "🟠 REDUCE",
    "sell": "🔴 SELL",
    "no_action": "⚪️ NO ACTION",
}


def _esc(s: Any) -> str:
    return html.escape(str(s)) if s is not None else ""


def format_alert(post: dict[str, Any], analysis: dict[str, Any]) -> str:
    analyst = analysis.get("analyst") or {}
    technical = analysis.get("technical") or {}
    pm = analysis.get("portfolio_manager") or {}
    tech_ctx = post.get("technical") or {}

    score = pm.get("relevance_score", 0)
    urgency = (pm.get("urgency") or "").upper() or "?"
    sentiment = (analyst.get("sentiment") or "neutral").lower()
    sentiment_label = _SENTIMENT_EMOJI.get(sentiment, "🟡") + " " + sentiment.title()
    action = (pm.get("recommendation") or "no_action").lower()
    action_label = _ACTION_LABEL.get(action, action.upper())

    ticker_label = post.get("ticker") or "—"
    name = post.get("name_en") or post.get("name_zh") or ""
    dissem = post.get("dissemination_count", 1)

    header_signal = "HIGH SIGNAL" if score >= 9 else ("MEDIUM SIGNAL" if score >= 7 else "SIGNAL")

    tech_lines: list[str] = []
    if tech_ctx:
        change = tech_ctx.get("change_pct", 0)
        change_emoji = "📉" if change < 0 else "📈"
        tech_lines.append(
            f"{change_emoji} <b>TECHNICAL:</b> {change}% today | "
            f"RSI {tech_ctx.get('rsi')} ({tech_ctx.get('rsi_signal')}) "
            f"| {tech_ctx.get('ma20_pct')}% vs MA20 ({tech_ctx.get('vs_ma20')})"
        )
    divergence = (technical.get("divergence") or "").lower()
    if divergence and divergence != "none":
        note = technical.get("divergence_note") or ""
        tech_lines.append(f"🔀 <b>DIVERGENCE:</b> {_esc(divergence)} — {_esc(note)}")

    translation = analysis.get("translation")
    body = []
    body.append(f"🔴 <b>{_esc(header_signal)}</b> — {_esc(post.get('source', '?'))} | "
                f"Score: {_esc(score)}/10 | {_esc(dissem)} source(s) in 2hrs")
    body.append("")
    body.append(f"📊 <b>STOCK:</b> ${_esc(ticker_label)} {_esc(name)}")
    body.append(f"💬 <b>SENTIMENT:</b> {sentiment_label} | "
                f"🏷️ {_esc((analyst.get('category') or 'other').upper())}")
    body.append(f"📡 <b>DISSEMINATION:</b> {_esc(analyst.get('dissemination_signal') or 'isolated')}")
    body.append("")
    body.extend(tech_lines)
    if tech_lines:
        body.append("")
    body.append("📝 <b>ANALYST</b>")
    body.append(_esc(analyst.get("summary") or ""))
    body.append(f"Credibility: {_esc(analyst.get('credibility') or '?')}. "
                f"New information: {'Yes' if analyst.get('is_new_information') else 'No'}.")
    if translation and translation != post.get("text"):
        body.append("")
        body.append(f"<i>Translation:</i> {_esc(translation)}")
    body.append("")
    body.append("━━━━━━━━━━━━━━━━━━━━")
    body.append("⚡ <b>PORTFOLIO MANAGER</b>")
    body.append("━━━━━━━━━━━━━━━━━━━━")
    body.append(f"<b>{_esc(action_label)}</b>")
    body.append(f"Confidence: {_esc(pm.get('confidence') or '?').title()} | "
                f"Horizon: {_esc(pm.get('time_horizon') or '?').title()} | Urgency: {_esc(urgency)}")
    body.append("")
    body.append(_esc(pm.get("reasoning") or ""))
    rules = pm.get("rules_checked")
    if rules:
        body.append(f"\n⚠️ Rules: {_esc(rules)}")
    conditions = pm.get("conditions")
    if conditions:
        body.append(f"⚠️ Condition: {_esc(conditions)}")
    body.append("━━━━━━━━━━━━━━━━━━━━")
    url = post.get("url")
    if url:
        body.append(f'\n<a href="{_esc(url)}">View Original</a>')
    body.append("\n⚠️ AI-generated. Not financial advice. Your call.")
    return "\n".join(body)


def send_alert(post: dict[str, Any], analysis: dict[str, Any]) -> int | None:
    return send_message(format_alert(post, analysis))


def test_connection() -> bool:
    if not BOT_TOKEN:
        return False
    try:
        r = requests.get(BASE_URL.format(token=BOT_TOKEN, method="getMe"), timeout=10)
        return r.ok and r.json().get("ok", False)
    except requests.RequestException:
        return False
