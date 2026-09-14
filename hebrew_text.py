"""Repair mojibake in Hebrew catalog text and make a phonetic English spelling."""

from __future__ import annotations

import re

HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
LATIN_RE = re.compile(r"[A-Za-z]")
SPLIT_RE = re.compile(r"\s*[/|–—]\s*")


def has_hebrew(text: str | None) -> bool:
    return bool(HEBREW_RE.search(text or ""))


def _hebrew_count(text: str) -> int:
    return len(HEBREW_RE.findall(text or ""))


def repair_text(text: str | None) -> str:
    """If a string is Hebrew decoded with the wrong charset, restore readable Hebrew."""
    original = str(text or "")
    if not original.strip() or has_hebrew(original):
        return original
    candidates = [original]
    for source, target in (
        ("latin-1", "utf-8"),
        ("cp1252", "utf-8"),
        ("latin-1", "cp1255"),
        ("cp1252", "cp1255"),
    ):
        try:
            candidates.append(original.encode(source).decode(target))
        except (UnicodeDecodeError, UnicodeEncodeError, LookupError):
            continue

    def score(value: str) -> tuple[int, int, int]:
        return (
            _hebrew_count(value),
            -value.count("\ufffd"),
            -(value.count("×") + value.count("Ã")),
        )

    best = max(candidates, key=score)
    if score(best)[0] > score(original)[0]:
        return re.sub(r"\s+", " ", best).strip()
    return original


def split_hebrew_latin(text: str | None) -> tuple[str, str]:
    """Split a mixed title into Hebrew and official Latin/English parts."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return "", ""
    chunks = [part.strip() for part in SPLIT_RE.split(value) if part.strip()]
    if len(chunks) <= 1:
        if has_hebrew(value):
            return value, ""
        if LATIN_RE.search(value):
            return "", value
        return value, ""
    hebrew: list[str] = []
    latin: list[str] = []
    leftover: list[str] = []
    for part in chunks:
        if has_hebrew(part):
            hebrew.append(part)
        elif LATIN_RE.search(part):
            latin.append(part)
        else:
            leftover.append(part)
    if leftover and not hebrew:
        hebrew.extend(leftover)
    elif leftover and not latin:
        latin.extend(leftover)
    return " / ".join(hebrew), " / ".join(latin)


def hebrew_phonetic(text: str | None) -> str:
    """Approximate Latin spelling of a Hebrew title. Not a translation."""
    from hebrew_phonetic_model import phonetic_title

    return phonetic_title(repair_text(text))
