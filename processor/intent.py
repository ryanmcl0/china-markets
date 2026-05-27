"""Lightweight intent routing for conversational messages.

`classify(message)` returns one of:
- "question"      — free-text Q&A (route to chat handler with full investor context)
- "portfolio"     — buy/sell/position change (route to config_writer)
- "config"        — limit/keyword/watchlist edit (route to config_writer)
- "unclear"       — bot asks for clarification
"""

from __future__ import annotations

import logging
from typing import Any

import llm

logger = logging.getLogger(__name__)


_LETTER_TO_INTENT = {
    "A": "question",
    "B": "portfolio",
    "C": "config",
    "D": "unclear",
}


def classify(message: str, config: dict[str, Any]) -> str:
    letter = llm.classify_intent(message, config)
    return _LETTER_TO_INTENT.get(letter, "unclear")
