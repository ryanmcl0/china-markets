"""Telegram Bot API over urllib (stdlib only — used by the plan step).

Outgoing text is Telegram HTML. ``render`` converts the small Markdown subset
Claude is asked to write (**bold**, `code`, [text](url)) into escaped HTML.
Nothing here logs message content: run logs are public.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram's hard limit is 4096; leave room for tags


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def chat_ids() -> list[str]:
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def _call(method: str, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    token = _token()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return None
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        API.format(token=token, method=method), data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Telegram's error description is safe to log; the request body is not.
        try:
            desc = json.loads(e.read()).get("description")
        except Exception:
            desc = ""
        logger.error("telegram %s failed: HTTP %s %s", method, e.code, desc)
    except (urllib.error.URLError, TimeoutError) as e:
        logger.error("telegram %s failed: %s", method, e)
    return None


# ── Rendering ────────────────────────────────────────────────────────────────

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def render(md: str) -> str:
    """Escape text and convert a small Markdown subset to Telegram HTML."""
    out = html.escape(md.strip(), quote=False)
    # The URL is already escaped by the line above; only quotes still need guarding.
    out = _LINK.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    # Markdown headings aren't supported by Telegram; show them bold.
    out = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", out, flags=re.MULTILINE)
    return out


_PRE_BLOCK = re.compile(r"<pre>.*?</pre>", re.DOTALL)


def _units(text: str) -> list[str]:
    """Lines, except that a whole <pre>…</pre> block counts as one unit."""
    units: list[str] = []
    pos = 0
    for m in _PRE_BLOCK.finditer(text):
        units += text[pos:m.start()].split("\n")[:-1] if m.start() > pos else []
        # Text on the same line before <pre> joins the block.
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[max(pos, line_start):m.start()]
        line_end = text.find("\n", m.end())
        line_end = len(text) if line_end == -1 else line_end
        units.append(prefix + text[m.start():line_end])
        pos = line_end + 1
    if pos <= len(text):
        units += text[pos:].split("\n")
    return units


def chunk(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split on line boundaries, never inside a <pre> block.

    Tags in our messages open and close on one line (or within one <pre>
    block), so every part stays valid Telegram HTML.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for unit in _units(text):
        while len(unit) > limit:  # pathological single unit
            parts.append(unit[:limit])
            unit = unit[limit:]
        if current and len(current) + len(unit) + 1 > limit:
            parts.append(current.rstrip("\n"))
            current = ""
        current += unit + "\n"
    if current.strip():
        parts.append(current.rstrip("\n"))
    return parts


# ── Send / receive ───────────────────────────────────────────────────────────

def send(html_text: str, chat_id: str | None = None, reply_to: int | None = None) -> bool:
    targets = [chat_id] if chat_id else chat_ids()
    ok = bool(targets)
    for cid in targets:
        for i, part in enumerate(chunk(html_text)):
            payload: dict[str, Any] = {
                "chat_id": cid,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_to and i == 0:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            resp = _call("sendMessage", payload)
            if not (resp and resp.get("ok")):
                # Most likely malformed HTML from a model reply — retry as plain text.
                payload.pop("parse_mode")
                payload["text"] = re.sub(r"<[^>]+>", "", part)
                resp = _call("sendMessage", payload)
            ok = ok and bool(resp and resp.get("ok"))
    logger.info("sent message (%d chars) to %d chat(s): %s", len(html_text), len(targets), "ok" if ok else "FAILED")
    return ok


def typing(chat_id: str) -> None:
    _call("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def pending_updates() -> list[dict[str, Any]]:
    """Unacknowledged updates. Does not advance the offset."""
    resp = _call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    return (resp or {}).get("result") or []


def acknowledge(last_update_id: int) -> None:
    _call("getUpdates", {"offset": last_update_id + 1, "timeout": 0})


def authorized_messages(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = set(chat_ids())
    out = []
    for u in updates:
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) in allowed and msg.get("text"):
            out.append(msg)
    return out
