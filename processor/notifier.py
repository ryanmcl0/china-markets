"""Telegram message formatting + send."""

from __future__ import annotations

import ast
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

    logger.info("OUTGOING MESSAGE CONTENT:\n%s", text)

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

_DISSEMINATION_LABEL = {
    "viral": "Viral — spreading fast",
    "spreading": "Spreading across sources",
    "isolated": "Isolated — single source",
}

_DIVERGENCE_LABEL = {
    "sentiment_leads_price": "Sentiment leads price",
    "price_leads_sentiment": "Price leads sentiment",
    "aligned": "Aligned",
    "none": "",
}


def _esc(s: Any) -> str:
    return html.escape(str(s)) if s is not None else ""


def _snake_to_title(s: str) -> str:
    return " ".join(w.capitalize() for w in s.split("_"))


def _dissemination_label(signal: str) -> str:
    key = (signal or "isolated").lower()
    return _DISSEMINATION_LABEL.get(key, _snake_to_title(key))


def _format_china_time(published_at: str | None) -> str:
    """Parse published_at and convert/format to China time."""
    if not published_at:
        return ""
    
    china_tz = pytz.timezone("Asia/Shanghai")
    try:
        # 1. Try ISO format (Reddit, RSS usually)
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        # If it has no timezone, assume UTC
        if dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)
        china_dt = dt.astimezone(china_tz)
    except ValueError:
        # 2. Try common formats from Chinese sources (Eastmoney often YYYY-MM-DD HH:MM:SS)
        try:
            formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
            dt_naive = None
            for fmt in formats:
                try:
                    dt_naive = datetime.strptime(published_at, fmt)
                    break
                except ValueError:
                    continue
            
            if dt_naive:
                # Eastmoney strings are already in China time
                china_dt = china_tz.localize(dt_naive)
            else:
                return f"({published_at})"
        except Exception:
            return f"({published_at})"

    return china_dt.strftime("%H:%M (CN)")


def _divergence_label(divergence: str) -> str:
    key = (divergence or "none").lower()
    return _DIVERGENCE_LABEL.get(key, _snake_to_title(key))


def _format_source(source: str) -> str:
    return _snake_to_title(source or "unknown")


def _sources_str(count: int) -> str:
    return f"{count} source" if count == 1 else f"{count} sources"


def _header_emoji(score: int) -> str:
    if score >= 9:
        return "🔴"
    if score >= 7:
        return "🟠"
    return "🟡"


def _format_rules(rules: Any) -> str:
    if not rules:
        return ""
    s = str(rules).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            items = ast.literal_eval(s)
            if isinstance(items, list):
                return "\n".join(f"• {item}" for item in items)
        except Exception:
            pass
    return s


def format_alert(post: dict[str, Any], analysis: dict[str, Any]) -> str:
    analyst = analysis.get("analyst") or {}
    technical = analysis.get("technical") or {}
    pm = analysis.get("portfolio_manager") or {}
    tech_ctx = post.get("technical") or {}

    score = pm.get("relevance_score", 0)
    action = (pm.get("recommendation") or "no_action").lower()
    action_label = _ACTION_LABEL.get(action, action.upper())
    confidence = (pm.get("confidence") or "").title()
    horizon = (pm.get("time_horizon") or "").title()

    ticker = post.get("ticker") or "—"
    name = post.get("name_en") or post.get("name_zh") or ticker
    source = _format_source(post.get("source") or "")
    dissem = post.get("dissemination_count", 1)
    china_time = _format_china_time(post.get("published_at"))

    # ── Header ──────────────────────────────────────────────
    body = []
    if tech_ctx and tech_ctx.get("change_pct") is not None:
        change = tech_ctx["change_pct"]
        change_emoji = "📉" if change < 0 else "📈"
        body.append(f"{change_emoji} <b>{_esc(name)}</b>  ·  {change:+.1f}%")
    else:
        body.append(f"📊 <b>{_esc(name)}</b>")

    subtitle_parts = [f"${_esc(ticker)}", f"Score {score}/10"]
    if china_time:
        subtitle_parts.append(china_time)
    subtitle_parts.append(_esc(source))
    
    if dissem > 1:
        subtitle_parts.append(_sources_str(dissem))
    body.append(f"<i>{' · '.join(subtitle_parts)}</i>")

    # ── What happened ────────────────────────────────────────
    summary = analyst.get("summary") or ""
    if summary:
        body.append("")
        body.append(_esc(summary))

    translation = analysis.get("translation")
    if translation and translation != post.get("text"):
        body.append(f"<i>{_esc(translation)}</i>")

    # ── Key facts (bullets) ──────────────────────────────────
    bullets: list[str] = []
    if tech_ctx:
        rsi = tech_ctx.get("rsi")
        rsi_sig = tech_ctx.get("rsi_signal") or ""
        vs_ma20 = tech_ctx.get("vs_ma20") or ""
        ma20_pct = abs(tech_ctx.get("ma20_pct") or 0)
        parts = []
        if rsi is not None:
            parts.append(f"RSI {rsi} ({rsi_sig})")
        if vs_ma20:
            parts.append(f"{ma20_pct}% {vs_ma20} 20-day MA")
        if parts:
            bullets.append(" · ".join(parts))

    divergence = (technical.get("divergence") or "none").lower()
    if divergence not in ("none", "aligned", ""):
        div_label = _divergence_label(divergence)
        note = technical.get("divergence_note") or ""
        line = div_label
        if note:
            line += f" — {note}"
        bullets.append(line)

    dissem_signal = (analyst.get("dissemination_signal") or "").lower()
    if dissem_signal == "viral":
        bullets.append("Spreading rapidly across multiple sources")

    if bullets:
        body.append("")
        for b in bullets:
            body.append(f"· {_esc(b)}")

    # ── Recommendation ───────────────────────────────────────
    body.append("")
    body.append("━━━━━━━━━━━━━━━━━━━━")
    pm_header = f"<b>{_esc(action_label)}</b>"
    meta = "  ·  ".join(p for p in [confidence, horizon] if p)
    if meta:
        pm_header += f"  ·  {_esc(meta)}"
    body.append(pm_header)
    body.append("━━━━━━━━━━━━━━━━━━━━")
    body.append("")
    body.append(_esc(pm.get("reasoning") or ""))

    conditions = pm.get("conditions")
    if conditions:
        body.append(f"\n⚠️ {_esc(conditions)}")

    url = post.get("url")
    if url:
        body.append(f'\n<a href="{_esc(url)}">View Original</a>')

    body.append("\n<i>AI-generated. Not financial advice. Your call.</i>")
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
