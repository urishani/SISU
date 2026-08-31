"""Repair mojibake in Hebrew catalog text and make a phonetic English spelling."""

from __future__ import annotations

import re

HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
NIQQUD_RE = re.compile(r"[\u05B0-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]")
LATIN_RE = re.compile(r"[A-Za-z]")
SPLIT_RE = re.compile(r"\s*[/|–—]\s*")

_LETTER = {
    "א": "",
    "ב": "v",
    "ג": "g",
    "ד": "d",
    "ה": "h",
    "ו": "v",
    "ז": "z",
    "ח": "ch",
    "ט": "t",
    "י": "y",
    "כ": "k",
    "ך": "kh",
    "ל": "l",
    "מ": "m",
    "ם": "m",
    "נ": "n",
    "ן": "n",
    "ס": "s",
    "ע": "",
    "פ": "p",
    "ף": "f",
    "צ": "ts",
    "ץ": "ts",
    "ק": "k",
    "ר": "r",
    "ש": "sh",
    "ת": "t",
}
_GERESH = {
    "g": "j",
    "z": "zh",
    "ts": "ch",
    "c": "ch",
}


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
    value = NIQQUD_RE.sub("", repair_text(text))
    if not value or not has_hebrew(value):
        return ""
    parts: list[str] = []
    for word in re.split(r"(\s+|[“”\"'():;,.!?-]+)", value):
        if not word:
            continue
        if not has_hebrew(word):
            parts.append(word)
            continue
        parts.append(_phonetic_word(word))
    compact = re.sub(r"\s+", " ", "".join(parts)).strip(" /-|")
    return compact


def _phonetic_word(word: str) -> str:
    letters = list(word)
    out: list[str] = []
    index = 0
    while index < len(letters):
        ch = letters[index]
        nxt = letters[index + 1] if index + 1 < len(letters) else ""
        if ch in {"א", "ע"}:
            nxt_is_vowel = nxt in {"ו", "י"}
            if not nxt_is_vowel:
                out.append("a")
            index += 1
            continue
        if ch == "ב":
            out.append("b" if index == 0 else "v")
            index += 1
            continue
        if ch == "ו" and nxt == "ו":
            out.append("v")
            index += 2
            continue
        if ch == "י" and nxt == "י":
            out.append("y")
            index += 2
            continue
        if ch == "ו":
            out.append("v" if index == 0 else "o")
            index += 1
            continue
        if ch == "י":
            out.append("y" if index == 0 else "i")
            index += 1
            continue
        if ch == "ה" and index == len(letters) - 1:
            index += 1
            continue
        mapped = _LETTER.get(ch, ch if not has_hebrew(ch) else "")
        if nxt in {"'", "׳", "’"} and mapped in _GERESH:
            mapped = _GERESH[mapped]
            index += 2
            out.append(mapped)
            continue
        out.append(mapped)
        index += 1
    roman = re.sub(r"(.)\1{2,}", r"\1\1", "".join(out))
    roman = re.sub(r"'+", "'", roman).strip("'")
    if not roman:
        return ""
    return roman[:1].upper() + roman[1:]
