"""Headless Claude Code (``claude -p``) for writing messages.

Authenticates with CLAUDE_CODE_OAUTH_TOKEN (a Claude subscription token from
``claude setup-token``) in Actions or on the NAS, or the local login when run
on a laptop. Claude gets web search/fetch only — no shell, no file writes — and
runs in an empty temp directory. Output goes to our process, never to logs.

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
    tools = "WebSearch,WebFetch" if web else ""
    cmd = [
        "claude", "-p",
        "--model", model or os.environ.get("MONITOR_CLAUDE_MODEL") or DEFAULT_MODEL,
        "--output-format", "json",
        "--max-turns", "25",
        "--disallowedTools", "Bash,Edit,Write,NotebookEdit,Task",
    ]
    if tools:
        cmd += ["--allowedTools", tools]
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=cwd
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
