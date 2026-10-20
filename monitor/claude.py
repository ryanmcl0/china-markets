"""Headless Claude Code (``claude -p``) for writing messages.

Authenticates with CLAUDE_CODE_OAUTH_TOKEN (a Claude subscription token from
``claude setup-token``) in Actions or on the NAS, or the local login when run
on a laptop. Claude gets web search, plus web fetch on the news and filings
sites below, and no other tools: no shell, no file reads or writes. It runs in
an empty temp directory without the Telegram secrets in its environment. The
data bundle includes text from strangers (news, filings, web pages), so this
limits what an injected instruction could reach or send anywhere. Output goes
to our process, never to logs.

If Claude is unavailable or fails, callers fall back to deterministic text.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_MODEL = "claude-sonnet-5-5"
TIMEOUT_S = 600

# WebFetch is allowed only on these sites (each also matches its subdomains).
# Anything else is denied, so a prompt injected via news or a web page can't
# send the data bundle to an arbitrary URL. Search results still cover the rest.
FETCH_DOMAINS = (
    # Exchanges and filings
    "cninfo.com.cn", "hkexnews.hk", "hkex.com.hk", "sse.com.cn", "szse.cn",
    # Chinese financial press
    "caixin.com", "caixinglobal.com", "yicai.com", "yicaiglobal.com", "cls.cn", "stcn.com", "eastmoney.com",
    "sina.com.cn", "21jingji.com", "jiemian.com", "thepaper.cn", "xinhuanet.com", "chinadaily.com.cn",
    # International press
    "reuters.com", "bloomberg.com", "ft.com", "scmp.com", "nikkei.com", "cnbc.com",
    # Government and energy-sector sources
    "gov.cn", "iea.org", "irena.org", "world-nuclear-news.org", "world-nuclear.org",
    "pv-magazine.com", "pv-tech.org", "energy-storage.news", "windpowermonthly.com",
    "rechargenews.com", "carbonbrief.org",
)
# Kept out of Claude's environment: it never needs them.
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_TOKEN", "GH_TOKEN", "MONITOR_CONFIG")


def web_permissions() -> str:
    fetch = [f"WebFetch(domain:{h})" for d in FETCH_DOMAINS for h in (d, f"*.{d}")]
    return ",".join(["WebSearch", *fetch])


def available() -> bool:
    return shutil.which("claude") is not None and os.environ.get("MONITOR_NO_CLAUDE") != "1"


def compose(task: str, context: str, extra: str = "", model: str | None = None, web: bool = True) -> str | None:
    """Run prompts/<task>.md + shared rules against the context. Returns Markdown or None."""
    if not available():
        return None
    prompt = "\n\n".join(
        p for p in [
            (PROMPTS / "style.md").read_text(encoding="utf-8"),
            (PROMPTS / f"{task}.md").read_text(encoding="utf-8"),
            context,
            extra,  # e.g. the user's question — goes last, after the data
        ] if p
    )
    cmd = [
        "claude", "-p",
        "--model", model or os.environ.get("MONITOR_CLAUDE_MODEL") or DEFAULT_MODEL,
        "--output-format", "json",
        "--max-turns", "25",
        # --tools is the full set that exists; everything else (Read, Bash, …) is absent.
        "--tools", "WebSearch,WebFetch" if web else "",
        "--disallowedTools", "Bash,Edit,Write,NotebookEdit,Task",
    ]
    if web:
        cmd += ["--allowedTools", web_permissions()]
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=cwd, env=env
            )
        except subprocess.TimeoutExpired:
            logger.error("claude timed out after %ss (task=%s)", TIMEOUT_S, task)
            return None
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # stderr can hold auth/usage errors; it never contains our prompt.
        logger.error("claude returned non-JSON (exit %s): %s", proc.returncode, proc.stderr[-500:])
        return None
    if result.get("is_error") or result.get("subtype") != "success":
        logger.error("claude failed (task=%s): subtype=%s", task, result.get("subtype"))
        return None
    text = (result.get("result") or "").strip()
    logger.info("claude ok (task=%s, %d chars, %s turns)", task, len(text), result.get("num_turns"))
    return text or None
