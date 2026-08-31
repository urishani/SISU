"""Paragraph direction hints for mixed Hebrew/English (logical order, like HTML)."""

from __future__ import annotations

import re

HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
RLM = "\u200F"
LRM = "\u200E"
RLE = "\u202B"
PDF = "\u202C"
MARKS_RE = re.compile(r"[\u200E\u200F\u202A-\u202E\u2066-\u2069]")


def has_hebrew(text: str | None) -> bool:
    return bool(HEBREW_RE.search(text or ""))


def strip_bidi_marks(text: str | None) -> str:
    return MARKS_RE.sub("", text or "")


def rtl_left_aligned(text: str | None) -> str:
    """Hebrew titles: RTL layout inside the field, left-justified in an LTR widget.

    LRM pins the block to the left; RLE makes mixed Hebrew, numbers, and English
    lay out as on the printed book.
    """
    raw = strip_bidi_marks(text).strip()
    if not raw or not has_hebrew(raw):
        return raw
    return f"{LRM}{RLE}{raw}{PDF}"


def description_for_text(text: str) -> tuple[str, str]:
    """Return one logical paragraph plus a tag. Wrapping is left to the text widget."""
    raw = (text or "").strip()
    if not raw:
        return "", "ltr"
    if has_hebrew(raw):
        return RLM + raw, "rtl"
    return raw, "ltr"
