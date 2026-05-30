"""Telegram bot — slash commands + conversational handler.

Slash commands cover the daily ops loop (`/status`, `/summary`, `/watchlist`,
`/pause`, `/add`, `/updatelimit`, `/acted`). Anything without a slash is
classified by intent.py and routed to either:
  - the free-text Q&A chat handler (with full investor context preloaded)
  - the structured portfolio-action extractor (writes config.yaml)

Destructive actions require a YES/NO confirmation reply.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config_writer
import db
import intent
import llm
import post_queue
from config_loader import load_config

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_CHAT_IDS = [
    c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()
]


def restricted(func):
    """Decorator to restrict access to authorized chat IDs."""

    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if chat_id not in AUTHORIZED_CHAT_IDS:
            logger.warning("Unauthorized access attempt from chat_id=%s", chat_id)
            if update.message:
                await update.message.reply_text("⛔ Unauthorized. This incident has been logged.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapped


# ──────────────────────────────────────────────────────────
# Session + pending-confirmation state (in-process)
# ──────────────────────────────────────────────────────────

def _get_session(chat_id: int | str) -> list[dict[str, str]]:
    return post_queue.get_session(chat_id)


def _save_session(chat_id: int | str, messages: list[dict[str, str]]) -> None:
    post_queue.save_session(chat_id, messages)


def _save_pending(chat_id: int | str, action: dict[str, Any]) -> None:
    post_queue.save_pending(chat_id, action)


def _pop_pending(chat_id: int | str) -> dict[str, Any] | None:
    return post_queue.pop_pending(chat_id)


def _is_paused() -> bool:
    return post_queue.is_paused()


# ──────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────

@restricted
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    queue_len = post_queue.get_queue().qsize()
    posts_today = db.post_count_today()
    health = post_queue.get_health()
    last_polls = {}
    for src in ("akshare", "reddit", "rss"):
        ts = health.get(src)
        last_polls[src] = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "never"
        )
    pause_until = post_queue.get_pause_until()
    pause_text = "no"
    if pause_until > datetime.now(timezone.utc).timestamp():
        pause_text = datetime.fromtimestamp(pause_until, tz=timezone.utc).isoformat()

    text = (
        "<b>Status</b>\n"
        f"Queue depth: {queue_len}\n"
        f"Posts today: {posts_today}\n"
        f"Paused until: {pause_text}\n"
        f"Last polls:\n"
        f"  • akshare: {last_polls['akshare']}\n"
        f"  • reddit:  {last_polls['reddit']}\n"
        f"  • rss:     {last_polls['rss']}\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@restricted
async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    min_score = config.get("notifications", {}).get("score_digest_only", 5)
    rows = db.posts_since(hours=24, min_score=min_score)
    if not rows:
        await update.message.reply_text("No posts above threshold in the last 24h.")
        return
    lines = [f"<b>Last 24h — {len(rows)} items</b>", ""]
    for r in rows[:30]:
        tickers = ", ".join(r.get("stocks_mentioned") or []) or "—"
        action = (r.get("action_recommendation") or "n/a").upper()
        lines.append(f"• [{r['relevance_score']}/10] {tickers} — {action}")
        s = r.get("analyst_summary") or ""
        if s:
            lines.append(f"  <i>{s[:180]}</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    watchlist = config.get("watchlist", [])
    tickers = [e["ticker"] for e in watchlist]
    last = db.last_signal_per_ticker(tickers)
    lines = ["<b>Watchlist</b>"]
    for e in watchlist:
        t = e["ticker"]
        currency = e.get("currency", "CNY")
        prefix = "HK$" if currency == "HKD" else "¥"
        status = e.get("status", "watching")
        weight = int(round(float(e.get("portfolio_weight", 0)) * 100))
        line = (
            f"• {t} {e.get('name_en','')} — {weight}%  | "
            f"limit {prefix}{e.get('gtc_limit')} | {status}"
        )
        sig = last.get(t)
        if sig:
            line += (
                f"\n   last: [{sig['relevance_score']}/10] "
                f"{(sig.get('action_recommendation') or 'n/a').upper()}"
            )
        pos = e.get("position")
        if pos:
            line += f"\n   pos: {pos.get('quantity')} @ {prefix}{pos.get('average_cost')}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /pause 2h or /pause 30m")
        return
    arg = ctx.args[0].lower()
    try:
        if arg.endswith("h"):
            secs = int(arg[:-1]) * 3600
        elif arg.endswith("m"):
            secs = int(arg[:-1]) * 60
        else:
            secs = int(arg)
    except ValueError:
        await update.message.reply_text("Could not parse duration. Try /pause 2h.")
        return
    until = int(datetime.now(timezone.utc).timestamp()) + secs
    post_queue.set_pause_until(until)
    until_dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
    await update.message.reply_text(f"⏸️ Paused until {until_dt.isoformat()} (UTC).")


@restricted
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /add TICKER name limit")
        return
    ticker = ctx.args[0]
    name = ctx.args[1] if len(ctx.args) > 1 else ticker
    try:
        limit = float(ctx.args[2]) if len(ctx.args) > 2 else None
    except ValueError:
        limit = None
    entry = {
        "ticker": ticker,
        "name_en": name,
        "name_zh": "",
        "exchange": "XSHG" if ticker.startswith("6") else ("HKEX" if len(ticker) <= 5 and ticker.isdigit() else "XSHE"),
        "currency": "CNY",
        "gtc_limit": limit,
        "portfolio_weight": 0.0,
        "status": "watching",
        "horizon": "5-10 years",
        "thesis": "",
        "tags": ["satellite"],
    }
    try:
        result = config_writer.add_stock(entry)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return
    await update.message.reply_text(f"✅ Added {result['added']} to watchlist.")


@restricted
async def cmd_updatelimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /updatelimit TICKER LIMIT")
        return
    ticker = ctx.args[0]
    try:
        new_limit = float(ctx.args[1])
    except ValueError:
        await update.message.reply_text("New limit must be numeric.")
        return
    try:
        result = config_writer.update_limit(ticker, new_limit)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return
    await update.message.reply_text(
        f"✅ {result['ticker']} limit: {result['old_limit']} → {result['new_limit']}"
    )


@restricted
async def cmd_acted(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /acted TICKER ACTION notes...")
        return
    ticker = ctx.args[0]
    action = ctx.args[1]
    notes = " ".join(ctx.args[2:]) or None
    try:
        db.record_action(ticker, action, notes)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return
    await update.message.reply_text(f"✅ Logged {action} for {ticker}.")


@restricted
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>Commands</b>\n"
        "/status — queue, posts today, last polls\n"
        "/summary — last 24h posts above threshold\n"
        "/watchlist — tickers, limits, last signals\n"
        "/pause 2h — silence notifications\n"
        "/add TICKER name limit — add to watchlist\n"
        "/updatelimit TICKER LIMIT — change a GTC limit\n"
        "/acted TICKER ACTION notes — log a decision\n"
        "\nOr just chat — questions and portfolio updates in plain English."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ──────────────────────────────────────────────────────────
# Conversational handler
# ──────────────────────────────────────────────────────────

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
    chat_id = str(msg.chat_id)

    # Authorization check
    if chat_id not in AUTHORIZED_CHAT_IDS:
        logger.warning("Unauthorized access attempt from chat_id=%s", chat_id)
        await msg.reply_text("⛔ Unauthorized. This incident has been logged.")
        return

    text = msg.text.strip()

    # YES/NO replies finalise a pending confirmation, if any.
    if text.upper() in {"YES", "Y"}:
        pending = _pop_pending(chat_id)
        if pending:
            await _execute_action(pending, msg)
            return
    if text.upper() in {"NO", "N", "CANCEL"}:
        if _pop_pending(chat_id):
            await msg.reply_text("Cancelled.")
            return

    config = load_config()
    try:
        intent_kind = intent.classify(text, config)
    except Exception:
        logger.exception("intent classification failed")
        intent_kind = "unclear"

    if intent_kind == "question":
        await _handle_question(text, chat_id, msg, config)
    elif intent_kind in {"portfolio", "config"}:
        await _handle_portfolio_change(text, chat_id, msg, config)
    else:
        await msg.reply_text(
            "Not sure what you meant. Try a /command or rephrase as a question."
        )


async def _handle_question(text: str, chat_id: int | str, msg, config: dict[str, Any]) -> None:
    history = _get_session(chat_id)
    history.append({"role": "user", "content": text})
    system_prompt = llm.build_investor_context(config)
    try:
        reply = llm.chat(history, system_prompt, config)
    except Exception as e:
        logger.exception("chat failed")
        await msg.reply_text(f"⚠️ LLM error: {e}")
        return
    history.append({"role": "assistant", "content": reply})
    _save_session(chat_id, history)
    for chunk in _chunk(reply, 3800):
        await msg.reply_text(chunk)


def _chunk(s: str, n: int) -> list[str]:
    return [s[i : i + n] for i in range(0, len(s), n)] or [""]


async def _handle_portfolio_change(text: str, chat_id: int | str, msg, config: dict[str, Any]) -> None:
    action = llm.extract_portfolio_action(text, config)
    if not action or not action.get("action_type"):
        await msg.reply_text("Couldn't extract an action from that. Try being more specific.")
        return

    if action.get("confirmation_required") or action.get("action_type") in {"remove_stock", "sell"}:
        _save_pending(chat_id, action)
        await msg.reply_text(
            f"⚠️ CONFIRM: {action.get('action_type')} on {action.get('ticker') or '(no ticker)'}?\n"
            "Reply YES to confirm or NO to cancel."
        )
        return

    await _execute_action(action, msg)


async def _execute_action(action: dict[str, Any], msg) -> None:
    try:
        kind = action["action_type"]
        ticker = action.get("ticker")
        if kind in {"buy", "add"} and ticker:
            result = config_writer.record_buy(
                ticker,
                quantity=float(action.get("quantity") or 0),
                price=float(action.get("price") or 0),
                currency=action.get("currency"),
            )
            pos = result["position"]
            await msg.reply_text(
                f"✅ Position updated — {ticker}\n"
                f"Status: holding\n"
                f"Quantity: {pos['quantity']}\n"
                f"Average cost: {pos['average_cost']} {pos['currency']}"
            )
        elif kind == "sell" and ticker:
            qty = action.get("quantity")
            result = config_writer.record_sell(ticker, float(qty) if qty else None)
            if result.get("exited"):
                await msg.reply_text(f"✅ {ticker} marked as exited.")
            else:
                await msg.reply_text(f"✅ {ticker} reduced; remaining: {result['remaining']}.")
        elif kind == "update_limit" and ticker:
            result = config_writer.update_limit(ticker, float(action["new_limit"]))
            await msg.reply_text(
                f"✅ {ticker} limit: {result['old_limit']} → {result['new_limit']}"
            )
        elif kind == "update_weight" and ticker:
            result = config_writer.update_weight(ticker, float(action["new_weight"]))
            await msg.reply_text(
                f"✅ {ticker} weight: {result['old_weight']} → {result['new_weight']}"
            )
        elif kind == "add_stock":
            entry = {
                "ticker": ticker,
                "name_en": action.get("name") or ticker,
                "name_zh": "",
                "exchange": "XSHG",
                "currency": action.get("currency") or "CNY",
                "gtc_limit": action.get("new_limit"),
                "portfolio_weight": action.get("new_weight") or 0.0,
                "status": "watching",
                "horizon": "5-10 years",
                "thesis": "",
                "tags": ["satellite"],
            }
            result = config_writer.add_stock(entry)
            await msg.reply_text(f"✅ Added {result['added']}.")
        elif kind == "remove_stock" and ticker:
            result = config_writer.remove_stock(ticker)
            await msg.reply_text(f"✅ Removed {result['removed']}. Monitoring stopped.")
        elif kind == "update_keyword":
            result = config_writer.add_keyword(
                action.get("keyword_category") or "sector_terms",
                action["keyword"],
            )
            await msg.reply_text(f"✅ Keyword update: {result}")
        else:
            await msg.reply_text(f"⚠️ Unsupported action: {kind}")
    except Exception as e:
        logger.exception("action execution failed")
        await msg.reply_text(f"❌ {e}")


# ──────────────────────────────────────────────────────────
# Application bootstrap
# ──────────────────────────────────────────────────────────

def run(stop_event: threading.Event) -> None:
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled")
        stop_event.wait()
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("updatelimit", cmd_updatelimit))
    app.add_handler(CommandHandler("acted", cmd_acted))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("bot polling started")
    app.run_polling(stop_signals=None, close_loop=False)
