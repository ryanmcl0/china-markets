"""The four tasks: close, filings, weekly, inbox."""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import claude, filings, market, news, report, schedule, signals, state as st, telegram
from .config import Stock, find, stocks as load_stocks

logger = logging.getLogger(__name__)

UK = ZoneInfo("Europe/London")

FOOTER = "\n\n<i>Written by Claude from prices, exchange filings and news · not financial advice</i>"

HELP = """<b>Commands</b>
/now — scoreboard: prices, moves vs index, distance from your limits
/stock <i>name</i> — one stock: numbers, recent filings, headlines, thesis
/filings [<i>name</i>] — material filings, last 14 days
/brief — this week's watchlist brief, now
/sector — China nuclear &amp; renewables market update, now
/limit <i>name price</i> — change a buy limit (e.g. /limit hailu 7.0)
/pause <i>4h</i> · /resume — mute alerts
/status — what ran when

Or just ask a question — "why is CATL down this week?", "should I lower my Hailu limit?" — and Claude answers with current data.
Replies come on the next check, usually within ~20 minutes."""


class Session:
    """One run: config, state and lazily fetched data shared across tasks."""

    def __init__(self, config: dict[str, Any], state: dict[str, Any], dry_run: bool = False):
        self.config = config
        self.state = state
        self.dry_run = dry_run
        self.stocks: list[Stock] = load_stocks(config, state.get("limit_overrides"))
        self._snapshot: tuple[dict[str, market.Metrics], dict[str, Any]] | None = None

    def snapshot(self) -> tuple[dict[str, market.Metrics], dict[str, Any]]:
        if self._snapshot is None:
            self._snapshot = market.snapshot(self.stocks)
        return self._snapshot

    @property
    def filing_codes(self) -> list[str]:
        return list(dict.fromkeys(s.filings_code for s in self.stocks if s.filings_code))

    def stock_for_code(self, code: str) -> Stock | None:
        return next((s for s in self.stocks if s.filings_code == code), None)

    def send(self, text: str, kind: str, title: str, chat_id: str | None = None,
             reply_to: int | None = None, force: bool = False) -> None:
        if not force and st.is_paused(self.state):
            logger.info("paused — not sending %s", kind)
            return
        if self.dry_run:
            print(f"\n──── [{kind}] {'→ ' + chat_id if chat_id else ''}\n{text}\n────")
        else:
            telegram.send(text, chat_id=chat_id, reply_to=reply_to)
        st.record_sent(self.state, kind, title)


def _cn_today() -> str:
    return datetime.now(report.CN).date().isoformat()


def _with_claude(body: str | None, fallback: str) -> str:
    return telegram.render(body) + FOOTER if body else fallback


# ── close ────────────────────────────────────────────────────────────────────

def close(sess: Session) -> None:
    now = datetime.now(timezone.utc)
    metrics, mkt = sess.snapshot()
    fresh = any(m.date == _cn_today() for m in metrics.values())
    if not fresh and now.hour < 11:
        logger.info("today's bars not published yet — will retry")
        return
    sess.state["tasks"]["close"] = now.date().isoformat()

    sigs = signals.evaluate(sess.stocks, metrics, sess.state["last_eval"], sess.state["last_signal"])
    logger.info("close: %d signal(s)", len(sigs))
    if not sigs:
        return

    codes = [s.stock.filings_code for s in sigs if s.stock.filings_code]
    recent = [f for f in filings.fetch_all(codes, days=10) if f.tier > 0]
    headlines = {s.stock.ticker: news.company(s.stock.short_zh, max_age_days=3) for s in sigs}
    ctx = report.context(sess.config, sess.stocks, metrics, mkt, filings=recent,
                         news_by_ticker=headlines, signals=sigs)
    body = claude.compose("close", ctx)

    bar_date = date.fromisoformat(max(s.metrics.date for s in sigs))
    header = f"🇨🇳 <b>China close · {bar_date:%a %d %b}</b>\n<i>{report.market_line(mkt)}</i>\n\n"
    sess.send(header + _with_claude(body, report.fallback_signals(sigs)), "close",
              ", ".join(s.stock.name_en for s in sigs))


# ── filings ──────────────────────────────────────────────────────────────────

def filings_check(sess: Session) -> None:
    sess.state["tasks"]["filings"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    lookback = int(os.environ.get("MONITOR_FILINGS_LOOKBACK_DAYS", "2"))
    cutoff = (date.today() - timedelta(days=lookback)).isoformat()
    found = filings.fetch_all(sess.filing_codes, days=lookback + 1)
    new = [f for f in found if f.tier == 1 and f.published >= cutoff and f.id not in sess.state["alerted_filings"]]
    logger.info("filings: %d fetched, %d new material", len(found), len(new))
    if not new or st.is_paused(sess.state):
        return

    texts = {f.id: filings.pdf_text(f) for f in new}
    metrics, mkt = sess.snapshot()
    involved = [s for s in (sess.stock_for_code(f.code) for f in new) if s]
    headlines = {s.ticker: news.company(s.short_zh, max_age_days=3) for s in involved}
    ctx = report.context(sess.config, sess.stocks, metrics, mkt, filings=new, filing_texts=texts,
                         news_by_ticker=headlines)
    body = claude.compose("filing", ctx)

    fallback = "\n".join(report.filing_line(f) for f in new)
    sess.send("📑 <b>New filing</b>\n\n" + _with_claude(body, fallback), "filing",
              "; ".join(f"{f.company} {f.category}" for f in new))
    for f in new:
        sess.state["alerted_filings"][f.id] = date.today().isoformat()


# ── weekly ───────────────────────────────────────────────────────────────────

def weekly(sess: Session, chat_id: str | None = None, mark: bool = True) -> None:
    metrics, mkt = sess.snapshot()
    recent = [f for f in filings.fetch_all(sess.filing_codes, days=7) if f.tier > 0]
    headlines = {s.ticker: news.company(s.short_zh, max_age_days=7) for s in sess.stocks}
    themes = {
        t["name"]: news.theme(t.get("queries", []), t.get("title_terms", []), max_age_days=14)
        for t in sess.config.get("themes", [])
    }
    ctx = report.context(sess.config, sess.stocks, metrics, mkt, filings=recent, news_by_ticker=headlines,
                         theme_news=themes, previous_brief=sess.state.get("last_weekly"))
    body = claude.compose("weekly", ctx)

    header = f"📊 <b>Weekly brief · {datetime.now(report.CN):%d %b %Y}</b>\n<i>{report.market_line(mkt, weekly=True)}</i>\n\n"
    if body:
        text = header + telegram.render(body) + "\n\n" + report.scoreboard(sess.stocks, metrics) + FOOTER
    else:
        material = [report.filing_line(f) for f in recent[:15]]
        text = header + report.scoreboard(sess.stocks, metrics)
        if material:
            text += "\n\n<b>Filings this week</b>\n" + "\n".join(material)
    sess.send(text, "weekly", "weekly brief", chat_id=chat_id, force=chat_id is not None)
    if mark:
        sess.state["tasks"]["weekly"] = schedule.week_key(datetime.now(timezone.utc))
        if body:
            sess.state["last_weekly"] = body


# ── sector ───────────────────────────────────────────────────────────────────

def sector(sess: Session, chat_id: str | None = None, mark: bool = True) -> None:
    """Sunday update on the whole China nuclear / renewables market, not just the watchlist."""
    metrics, mkt = sess.snapshot()
    themes = {
        t["name"]: news.theme(t.get("queries", []), t.get("title_terms", []), max_age_days=8)
        for t in sess.config.get("sector_themes", []) + sess.config.get("themes", [])
    }
    ctx = report.context(sess.config, sess.stocks, metrics, mkt, theme_news=themes,
                         previous_brief=sess.state.get("last_sector"), previous_label="sector update")
    body = claude.compose("sector", ctx)

    header = f"🌏 <b>China energy market · week to {datetime.now(UK):%d %b}</b>\n\n"
    if body:
        text = header + telegram.render(body) + FOOTER
    else:
        lines = []
        for name, arts in themes.items():
            if arts:
                lines.append(f"<b>{html.escape(name)}</b>")
                lines += [f'{a.published[5:10]} <a href="{html.escape(a.url)}">{html.escape(a.title)}</a>' for a in arts[:5]]
                lines.append("")
        text = header + ("\n".join(lines).strip() or "No sector news found this week.")
    sess.send(text, "sector", "sector update", chat_id=chat_id, force=chat_id is not None)
    if mark:
        sess.state["tasks"]["sector"] = schedule.week_key(datetime.now(timezone.utc))
        if body:
            sess.state["last_sector"] = body


# ── inbox ────────────────────────────────────────────────────────────────────

def inbox(sess: Session) -> None:
    updates = telegram.pending_updates()
    if not updates:
        return
    try:
        for msg in telegram.authorized_messages(updates):
            chat_id, mid = str(msg["chat"]["id"]), msg.get("message_id")
            try:
                reply = handle(sess, msg["text"].strip(), chat_id)
            except Exception:
                logger.exception("handling a message failed")
                reply = "Sorry — that failed on my side. Try again, or /help."
            if reply:
                sess.send(reply, "reply", "reply", chat_id=chat_id, reply_to=mid, force=True)
    finally:
        telegram.acknowledge(max(u["update_id"] for u in updates))
    logger.info("inbox: handled %d update(s)", len(updates))


def _duration(arg: str) -> timedelta | None:
    m = re.fullmatch(r"(\d+)\s*([mhd])", arg.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)


def handle(sess: Session, text: str, chat_id: str) -> str | None:
    if not text.startswith("/"):
        return ask(sess, text, chat_id)

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd in ("/help", "/start"):
        return HELP

    if cmd == "/now":
        metrics, mkt = sess.snapshot()
        return f"<i>{report.market_line(mkt)}</i>\n\n" + report.scoreboard(sess.stocks, metrics)

    if cmd == "/stock":
        s = find(sess.stocks, arg)
        if not s:
            return "Which stock? e.g. /stock hailu"
        metrics, _ = sess.snapshot()
        fl = [f for f in filings.fetch(s.filings_code, days=30) if f.tier > 0] if s.filings_code else []
        return report.stock_detail(s, metrics.get(s.ticker), fl, news.company(s.short_zh, max_age_days=10))

    if cmd == "/filings":
        codes = sess.filing_codes
        if arg:
            s = find(sess.stocks, arg)
            if not s or not s.filings_code:
                return "I don't have filings for that one."
            codes = [s.filings_code]
        fl = [f for f in filings.fetch_all(codes, days=14) if f.tier > 0]
        if not fl:
            return "No material filings in the last 14 days."
        return "<b>Material filings, last 14 days</b>\n" + "\n".join(report.filing_line(f) for f in fl[:25])

    if cmd == "/brief":
        weekly(sess, chat_id=chat_id, mark=False)
        return None

    if cmd == "/sector":
        sector(sess, chat_id=chat_id, mark=False)
        return None

    if cmd == "/limit":
        parts = arg.rsplit(" ", 1)
        s = find(sess.stocks, parts[0]) if len(parts) == 2 else None
        try:
            price = float(parts[1].lstrip("¥$HK")) if s else None
        except ValueError:
            price = None
        if not s or price is None or price <= 0:
            return "Usage: /limit <i>name price</i>, e.g. /limit hailu 7.0"
        old = s.limit
        sess.state["limit_overrides"][s.ticker] = price
        return f"{html.escape(s.name_en)} limit: {s.price(old)} → {s.price(price)}"

    if cmd == "/pause":
        d = _duration(arg or "4h")
        if not d:
            return "Usage: /pause 4h (or 30m, 2d)"
        until = datetime.now(timezone.utc) + d
        sess.state["pause_until"] = until.isoformat()
        return f"Alerts paused until {until.astimezone(UK):%a %H:%M} UK time. Replies still work. /resume to undo."

    if cmd == "/resume":
        sess.state["pause_until"] = None
        return "Alerts resumed."

    if cmd == "/status":
        t = sess.state["tasks"]
        overrides = ", ".join(f"{k} {v}" for k, v in sess.state["limit_overrides"].items()) or "none"
        return (
            f"Last close check: {t.get('close', 'never')}\nLast filings check: {t.get('filings', 'never')}\n"
            f"Last weekly: {t.get('weekly', 'never')}\nPaused: {'yes' if st.is_paused(sess.state) else 'no'}\n"
            f"Claude: {'available' if claude.available() else 'NOT available — messages are facts only'}\n"
            f"Limit overrides: {html.escape(overrides)}\nWatching {len(sess.stocks)} stocks."
        )

    return "Unknown command. /help"


def ask(sess: Session, question: str, chat_id: str) -> str:
    if not claude.available():
        return "Claude isn't available right now, so I can't answer free-text questions. Try /now or /stock."
    telegram.typing(chat_id)
    metrics, mkt = sess.snapshot()
    recent = [f for f in filings.fetch_all(sess.filing_codes, days=14) if f.tier > 0]
    headlines = {s.ticker: news.company(s.short_zh, max_age_days=7) for s in sess.stocks}
    themes = {
        t["name"]: news.theme(t.get("queries", []), t.get("title_terms", []), max_age_days=21)
        for t in sess.config.get("themes", [])
    }
    ctx = report.context(sess.config, sess.stocks, metrics, mkt, filings=recent, news_by_ticker=headlines,
                         theme_news=themes, previous_brief=sess.state.get("last_weekly"))
    body = claude.compose("ask", ctx, extra=f"## The investor's message\n{question}")
    return telegram.render(body) + FOOTER if body else "Sorry — Claude didn't manage an answer this time. Try again shortly."
