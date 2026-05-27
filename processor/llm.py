"""Ollama client + prompt builder.

One call does four things in a single pass per post: translate, score,
summarise, recommend. The prompt is split into an explicit "analyst"
section and a "portfolio manager" section, which empirically produces
cleaner, less biased reasoning from a 7B model than a flat prompt.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_TIMEOUT = 300


# ──────────────────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────────────────

def _format_watchlist(watchlist: list[dict[str, Any]]) -> str:
    """Render the watchlist as the bulleted portfolio block used in the prompt."""
    tiers = {"core": [], "satellite": [], "speculative": []}
    for entry in watchlist:
        tags = entry.get("tags", []) or []
        if "core" in tags:
            tier = "core"
        elif "speculative" in tags:
            tier = "speculative"
        else:
            tier = "satellite"
        currency = entry.get("currency", "CNY")
        prefix = "¥"
        if currency == "HKD":
            prefix = "HK$"
        elif currency == "USD":
            prefix = "$"
        weight_pct = int(round(float(entry.get("portfolio_weight", 0)) * 100))
        line = (
            f"- {entry['ticker']} {entry.get('name_en', '')} ({entry.get('name_zh', '')}) "
            f"| {weight_pct}% | limit {prefix}{entry.get('gtc_limit')} "
            f"| status={entry.get('status', 'watching')}"
        )
        tiers[tier].append(line)

    parts = []
    if tiers["core"]:
        parts.append("CORE POSITIONS (12-15%):\n" + "\n".join(tiers["core"]))
    if tiers["satellite"]:
        parts.append("SATELLITE POSITIONS (8-10%):\n" + "\n".join(tiers["satellite"]))
    if tiers["speculative"]:
        parts.append("SPECULATIVE POSITIONS (5-7%):\n" + "\n".join(tiers["speculative"]))
    return "\n\n".join(parts)


def build_investor_context(config: dict[str, Any]) -> str:
    """Construct the investor + watchlist + rules block prepended to prompts."""
    inv = config.get("investor", {})
    rules = config.get("investing_rules", [])
    watchlist = config.get("watchlist", [])
    sector = config.get("sector", {})

    rules_block = "\n".join(f"- {r}" for r in rules)

    return (
        f"SECTOR: {sector.get('name', 'China Energy Market')} — {inv.get('horizon', '5-10 years')} horizon\n\n"
        f"PORTFOLIO (ranked by conviction, weight shown):\n{_format_watchlist(watchlist)}\n\n"
        f"INVESTOR: {inv.get('type', '')} ({inv.get('country', '')}). "
        f"{inv.get('horizon', '')} horizon. Bias: {inv.get('bias', '')}. "
        f"Risk tolerance: {inv.get('risk_tolerance', '')}.\n"
        f"Notes: {(inv.get('notes') or '').strip()}\n\n"
        f"INVESTING RULES (check every recommendation against these):\n{rules_block}\n"
    )


SYSTEM_PROMPT = (
    "You are a financial analyst specialising in Chinese energy markets, "
    "advising a long-term UK retail investor. Always check recommendations "
    "against the INVESTING RULES before outputting. Respond in JSON only — "
    "no preamble, no markdown."
)


def build_monitoring_prompt(post: dict[str, Any], config: dict[str, Any]) -> str:
    investor_block = build_investor_context(config)
    tech = post.get("technical") or {}
    ticker = post.get("ticker") or "(unknown)"

    if tech:
        tech_line = (
            f"TECHNICAL CONTEXT for {ticker}:\n"
            f"RSI: {tech.get('rsi')} ({tech.get('rsi_signal')}) | "
            f"Price: {tech.get('current_price')} | "
            f"{tech.get('ma20_pct')}% vs 20-day MA ({tech.get('vs_ma20')})"
        )
    else:
        tech_line = f"TECHNICAL CONTEXT for {ticker}: unavailable"

    dissemination = int(post.get("dissemination_count") or 1)

    schema = """{
  "translation": "Full English translation if Chinese, else null",
  "analyst": {
    "summary": "2-3 sentence objective assessment of what happened",
    "credibility": "high/medium/low",
    "is_new_information": true,
    "stocks_mentioned": ["tickers from watchlist only"],
    "sentiment": "bullish/bearish/neutral",
    "category": "earnings/contract/regulatory/technical/sentiment/macro/other",
    "dissemination_signal": "viral/spreading/isolated"
  },
  "technical": {
    "rsi_context": "oversold/overbought/neutral",
    "price_vs_ma20": "above/below",
    "divergence": "sentiment_leads_price/price_leads_sentiment/aligned/none",
    "divergence_note": "Optional: brief explanation if divergence detected"
  },
  "portfolio_manager": {
    "relevance_score": 1,
    "urgency": "high/medium/low",
    "notify": false,
    "recommendation": "buy/add/hold/reduce/sell/watch/wait_for_dip/no_action",
    "reasoning": "2-3 sentences referencing analyst findings, technical context, and the investor's GTC limits — note any rule violations",
    "confidence": "high/medium/low",
    "time_horizon": "immediate/days/weeks/months",
    "conditions": "Optional: specific price level or event to wait for",
    "rules_checked": "Confirm which investing rules were considered"
  }
}"""

    return f"""{investor_block}

{tech_line}

DISSEMINATION: This content has appeared across {dissemination} source(s) in the last 2 hours.

───────────────────────────────────────
STEP 1 — ANALYST
Assess the content objectively. What happened? Is it credible?
Is this new information or already known? What is the market context?
───────────────────────────────────────
STEP 2 — PORTFOLIO MANAGER
Given the analyst assessment, investor profile, technical context,
and investing rules — what should the investor do?
───────────────────────────────────────

Respond with exactly this JSON shape (replace example values):
{schema}

SOURCE: {post.get("source", "unknown")}
CONTENT:
{post.get("text", "").strip()}
"""


# ──────────────────────────────────────────────────────────
# Ollama HTTP client
# ──────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from a possibly-noisy LLM response."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _generate(prompt: str, model: str, temperature: float, max_tokens: int, system: str | None = None) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if system:
        payload["system"] = system

    r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json().get("response", "")


def analyse_post(post: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Run the full monitoring prompt and return the parsed JSON output."""
    llm_cfg = config.get("llm", {})
    model = llm_cfg.get("model", "qwen2.5:3b-instruct-q4_K_M")
    logger.info("analysing post with model=%s", model)
    prompt = build_monitoring_prompt(post, config)
    raw = _generate(
        prompt,
        model=model,
        temperature=llm_cfg.get("temperature", 0.2),
        max_tokens=llm_cfg.get("max_tokens", 1024),
        system=SYSTEM_PROMPT,
    )
    parsed = _extract_json(raw)
    if parsed is None:
        logger.warning("LLM returned non-JSON output, falling back to no_action")
        return {
            "translation": None,
            "analyst": {
                "summary": (raw or "(empty)")[:400],
                "credibility": "low",
                "is_new_information": False,
                "stocks_mentioned": [],
                "sentiment": "neutral",
                "category": "other",
                "dissemination_signal": "isolated",
            },
            "technical": {"divergence": "none"},
            "portfolio_manager": {
                "relevance_score": 1,
                "urgency": "low",
                "notify": False,
                "recommendation": "buy/add/hold/reduce/sell/watch/wait_for_dip/no_action",
                "reasoning": "LLM output unparseable.",
                "confidence": "low",
                "time_horizon": "days",
                "rules_checked": "",
            },
        }
    return parsed


def classify_intent(message: str, config: dict[str, Any]) -> str:
    """Return one of: A, B, C, D — see processor/intent.py for routing."""
    llm_cfg = config.get("llm", {})
    model = llm_cfg.get("model", "qwen2.5:3b-instruct-q4_K_M")
    prompt = f"""CLASSIFY this message into exactly one category:
A — general question about markets, stocks, or portfolio
B — portfolio update (trade executed, position changed)
C — config change (update limit, add stock, change keyword)
D — other / unclear

Respond with one letter only.

MESSAGE: {message.strip()}
"""
    raw = _generate(
        prompt,
        model=model,
        temperature=0.0,
        max_tokens=4,
    )
    letter = (raw or "").strip().upper()[:1]
    return letter if letter in {"A", "B", "C", "D"} else "D"


def chat(messages: list[dict[str, str]], system_prompt: str, config: dict[str, Any]) -> str:
    """Conversational chat using the /api/chat endpoint."""
    llm_cfg = config.get("llm", {})
    model = llm_cfg.get("model", "qwen2.5:3b-instruct-q4_K_M")
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "options": {
            "temperature": llm_cfg.get("temperature", 0.2),
            "num_predict": llm_cfg.get("max_tokens", 1024),
        },
    }
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "")


def extract_portfolio_action(message: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a plain-English portfolio update into a structured action dict."""
    llm_cfg = config.get("llm", {})
    model = llm_cfg.get("model", "qwen2.5:3b-instruct-q4_K_M")
    prompt = f"""EXTRACT the portfolio action from this message. Respond with JSON only.

Schema:
{{
  "action_type": "buy/sell/add/update_limit/add_stock/remove_stock/update_keyword/update_weight",
  "ticker": "string or null",
  "name": "string or null",
  "quantity": null,
  "price": null,
  "currency": "CNY/HKD/USD/GBP or null",
  "new_limit": null,
  "new_weight": null,
  "keyword": null,
  "keyword_category": "tickers/companies_en/companies_zh/sector_terms or null",
  "confirmation_required": false
}}

Set confirmation_required to true if the action removes data (remove_stock, sell-all).
MESSAGE: {message.strip()}
"""
    raw = _generate(
        prompt,
        model=model,
        temperature=0.0,
        max_tokens=512,
    )
    return _extract_json(raw)
