"""Paragraph direction hints for mixed Hebrew/English (logical order, like HTML)."""

from __future__ import annotations

import re

HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
RLM = "\u200F"


def has_hebrew(text: str | None) -> bool:
    return bool(HEBREW_RE.search(text or ""))


def description_for_text(text: str) -> tuple[str, str]:
    """Return one logical paragraph plus a tag. Wrapping is left to the text widget."""
    raw = (text or "").strip()
    if not raw:
        return "", "ltr"
    if has_hebrew(raw):
        return RLM + raw, "rtl"
    return raw, "ltr"
