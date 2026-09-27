"""Add Hebrew nikud (menukad) and spell English phonetics from those vowels."""

from __future__ import annotations

import json
import re
from pathlib import Path

SHEVA = "\u05B0"
HATAF_SEGOL = "\u05B1"
HATAF_PATAH = "\u05B2"
HATAF_QAMATS = "\u05B3"
HIRIQ = "\u05B4"
TSERE = "\u05B5"
SEGOL = "\u05B6"
PATAH = "\u05B7"
QAMATS = "\u05B8"
HOLAM = "\u05B9"
HOLAM_HASER = "\u05BA"
QUBUTS = "\u05BB"
DAGESH = "\u05BC"
SHIN_DOT = "\u05C1"
SIN_DOT = "\u05C2"
QAMATS_QATAN = "\u05C7"

NIQQUD_RE = re.compile(r"[\u05B0-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]")
HEBREW_LETTER_RE = re.compile(r"[\u05D0-\u05EA]")
HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
SPLIT_KEEP_RE = re.compile(r"(\s+|[“”\"():;,.!?]+)")
MAQAF_RE = re.compile(r"[־\-]")
_LEXICON_PATH = Path(__file__).with_name("hebrew_phonetic_lexicon.json")

_VOWEL_MARK = {
    SHEVA: "e",
    HATAF_SEGOL: "e",
    HATAF_PATAH: "a",
    HATAF_QAMATS: "o",
    HIRIQ: "i",
    TSERE: "e",
    SEGOL: "e",
    PATAH: "a",
    QAMATS: "a",
    HOLAM: "o",
    HOLAM_HASER: "o",
    QUBUTS: "u",
    QAMATS_QATAN: "o",
}

_ROMAN_VOWEL = {
    "a": PATAH,
    "e": SEGOL,
    "i": HIRIQ,
    "o": HOLAM,
    "u": QUBUTS,
}

_DIGRAPH = ("ch", "sh", "ts", "kh", "zh")
_BGDKPT = {"ב", "ג", "ד", "כ", "פ", "ת", "ך", "ף"}

_PREFIXES: tuple[tuple[str, str], ...] = (
    ("וש", "וְשֶׁ"),
    ("שה", "שֶׁהַ"),
    ("כש", "כְּשֶׁ"),
    ("וה", "וְהַ"),
    ("וב", "וּבְ"),
    ("ול", "וּלְ"),
    ("וכ", "וּכְ"),
    ("ומ", "וּמְ"),
    ("מש", "מִשֶּׁ"),
    ("מה", "מֵהַ"),
    ("לה", "לְהַ"),
    ("בש", "בְּשֶׁ"),
    ("ה", "הַ"),
    ("ו", "וְ"),
    ("ב", "בְּ"),
    ("כ", "כְּ"),
    ("ל", "לְ"),
    ("מ", "מִ"),
    ("ש", "שֶׁ"),
)

# Standard pointing for catalog words. Unpointed key → menukad.
_NIKUD_WORDS: dict[str, str] = {
    "א": "א",
    "אב": "אָב",
    "אבא": "אַבָּא",
    "אביב": "אָבִיב",
    "אבות": "אָבוֹת",
    "אגדה": "אַגָּדָה",
    "אגדת": "אַגָּדַת",
    "אגדות": "אַגָּדוֹת",
    "אדם": "אָדָם",
    "אדמה": "אֲדָמָה",
    "אהבה": "אַהֲבָה",
    "אהבת": "אַהֲבַת",
    "אהוב": "אָהוּב",
    "אהובי": "אֲהוּבִי",
    "או": "אוֹ",
    "אוויר": "אֲוִיר",
    "אומץ": "אֹמֶץ",
    "אור": "אוֹר",
    "אז": "אָז",
    "אחד": "אֶחָד",
    "אחות": "אָחוֹת",
    "אחרון": "אַחֲרוֹן",
    "אחרונה": "אַחֲרוֹנָה",
    "אחרי": "אַחֲרֵי",
    "אחרים": "אֲחֵרִים",
    "אחרת": "אַחֶרֶת",
    "אחת": "אַחַת",
    "אי": "אִי",
    "אין": "אֵין",
    "איש": "אִישׁ",
    "אישי": "אִישִׁי",
    "אישה": "אִשָּׁה",
    "איך": "אֵיךְ",
    "אימא": "אִימָּא",
    "אם": "אִם",
    "אמא": "אִמָּא",
    "אמונה": "אֱמוּנָה",
    "אמת": "אֱמֶת",
    "אני": "אֲנִי",
    "אנחנו": "אֲנַחְנוּ",
    "אנשים": "אֲנָשִׁים",
    "אנשי": "אַנְשֵׁי",
    "אפשר": "אֶפְשָׁר",
    "אף": "אַף",
    "ארץ": "אֶרֶץ",
    "אש": "אֵשׁ",
    "את": "אֵת",
    "אתה": "אַתָּה",
    "בא": "בָּא",
    "בית": "בַּיִת",
    "בן": "בֵּן",
    "בנים": "בָּנִים",
    "בנות": "בָּנוֹת",
    "בוקר": "בֹּקֶר",
    "בת": "בַּת",
    "גדול": "גָּדוֹל",
    "גדולה": "גְּדוֹלָה",
    "גם": "גַּם",
    "גן": "גַּן",
    "גשר": "גֶּשֶׁר",
    "דבר": "דָּבָר",
    "דברים": "דְּבָרִים",
    "דם": "דָּם",
    "דרך": "דֶּרֶךְ",
    "דבש": "דְּבַשׁ",
    "הוא": "הוּא",
    "היא": "הִיא",
    "היה": "הָיָה",
    "הם": "הֵם",
    "זאת": "זֹאת",
    "זה": "זֶה",
    "זהב": "זָהָב",
    "זהות": "זְהוּת",
    "זמן": "זְמַן",
    "חבר": "חָבֵר",
    "חברה": "חֶבְרָה",
    "חברים": "חֲבֵרִים",
    "חברות": "חֲבֵרוֹת",
    "חיים": "חַיִּים",
    "חכם": "חָכָם",
    "חכמה": "חָכְמָה",
    "חלק": "חֵלֶק",
    "חתול": "חָתוּל",
    "חלב": "חָלָב",
    "חלום": "חֲלוֹם",
    "טוב": "טוֹב",
    "טובה": "טוֹבָה",
    "יד": "יָד",
    "יהודי": "יְהוּדִי",
    "יהודים": "יְהוּדִים",
    "יהודית": "יְהוּדִית",
    "יום": "יוֹם",
    "יומן": "יוֹמָן",
    "ימי": "יְמֵי",
    "ימים": "יָמִים",
    "ילד": "יֶלֶד",
    "ילדה": "יַלְדָּה",
    "ילדים": "יְלָדִים",
    "ים": "יָם",
    "יש": "יֵשׁ",
    "ישראל": "יִשְׂרָאֵל",
    "ירושלים": "יְרוּשָׁלַיִם",
    "כאן": "כָּאן",
    "כוח": "כֹּחַ",
    "כוכב": "כּוֹכָב",
    "כולם": "כֻּלָּם",
    "כי": "כִּי",
    "ככה": "כָּכָה",
    "כל": "כֹּל",
    "כלב": "כֶּלֶב",
    "כמו": "כְּמוֹ",
    "כמה": "כַּמָּה",
    "כן": "כֵּן",
    "כסף": "כֶּסֶף",
    "לא": "לֹא",
    "לב": "לֵב",
    "לו": "לוֹ",
    "לי": "לִי",
    "לילה": "לַיְלָה",
    "למה": "לָמָּה",
    "מדריך": "מַדְרִיךְ",
    "מים": "מַיִם",
    "מלך": "מֶלֶךְ",
    "מלחמה": "מִלְחָמָה",
    "מסע": "מַסָּע",
    "מקום": "מָקוֹם",
    "משפחה": "מִשְׁפָּחָה",
    "משחק": "מִשְׂחָק",
    "מה": "מָה",
    "מי": "מִי",
    "נער": "נַעַר",
    "נערה": "נַעֲרָה",
    "נפש": "נֶפֶשׁ",
    "נשים": "נָשִׁים",
    "סוד": "סוֹד",
    "סוף": "סוֹף",
    "ספר": "סֵפֶר",
    "ספרים": "סְפָרִים",
    "ספריה": "סִפְרִיָּה",
    "ספרייה": "סִפְרִיָּה",
    "סופר": "סוֹפֵר",
    "עולם": "עוֹלָם",
    "עוד": "עוֹד",
    "עד": "עַד",
    "על": "עַל",
    "עיר": "עִיר",
    "עץ": "עֵץ",
    "עין": "עַיִן",
    "עם": "עִם",
    "פעם": "פַּעַם",
    "פרק": "פֶּרֶק",
    "פרקי": "פִּרְקֵי",
    "צל": "צֵל",
    "קול": "קוֹל",
    "קטן": "קָטָן",
    "קסם": "קֶסֶם",
    "ראש": "רֹאשׁ",
    "רב": "רַב",
    "רוח": "רוּחַ",
    "רק": "רַק",
    "של": "שֶׁל",
    "שלו": "שֶׁלּוֹ",
    "שלי": "שֶׁלִּי",
    "שלך": "שֶׁלְּךָ",
    "שלנו": "שֶׁלָּנוּ",
    "שם": "שָׁם",
    "שמש": "שֶׁמֶשׁ",
    "שנה": "שָׁנָה",
    "שיר": "שִׁיר",
    "שירים": "שִׁירִים",
    "שלום": "שָׁלוֹם",
    "שקט": "שֶׁקֶט",
    "תורה": "תּוֹרָה",
    "תקווה": "תִּקְוָה",
    "חרבות": "חַרְבוֹת",
    "ברזל": "בַּרְזֶל",
    "סיפורים": "סִפּוּרִים",
    "סיפור": "סִפּוּר",
}


def has_nikud(text: str | None) -> bool:
    return bool(NIQQUD_RE.search(str(text or "")))


def strip_nikud(text: str | None) -> str:
    return NIQQUD_RE.sub("", str(text or "")).strip()


def _is_mark(ch: str) -> bool:
    code = ord(ch)
    return 0x05B0 <= code <= 0x05BD or code in {0x05BF, 0x05C1, 0x05C2, 0x05C4, 0x05C5, 0x05C7}


def _clusters(text: str) -> list[tuple[str, str, bool]]:
    items: list[tuple[str, str, bool]] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if _is_mark(ch):
            index += 1
            continue
        if not HEBREW_LETTER_RE.match(ch):
            items.append((ch, "", True))
            index += 1
            continue
        marks: list[str] = []
        index += 1
        while index < len(text) and _is_mark(text[index]):
            marks.append(text[index])
            index += 1
        items.append((ch, "".join(marks), False))
    return items


def _vowel_of(marks: str, *, sheva_na: bool = False) -> str:
    for mark, sound in _VOWEL_MARK.items():
        if mark == SHEVA:
            continue
        if mark in marks:
            return sound
    if SHEVA in marks and sheva_na:
        return "e"
    return ""


def _cons_of(letter: str, marks: str, *, first: bool, last: bool) -> str:
    if letter == "ו":
        if HOLAM in marks or HOLAM_HASER in marks:
            return ""
        if DAGESH in marks and not any(mark in marks for mark in _VOWEL_MARK if mark not in {SHEVA}):
            return ""
        return "v"
    if letter == "י":
        return "y" if first else ""
    if letter in {"א", "ע"}:
        return ""
    if letter == "ה" and last and not _vowel_of(marks):
        return ""
    if letter == "ש":
        if SIN_DOT in marks:
            return "s"
        return "sh"
    if letter == "ב":
        return "b" if DAGESH in marks or first else "v"
    if letter == "פ":
        return "p" if DAGESH in marks or first else "f"
    if letter in {"כ", "ך"}:
        return "k" if DAGESH in marks else "kh"
    mapping = {
        "ג": "g",
        "ד": "d",
        "ה": "h",
        "ז": "z",
        "ח": "ch",
        "ט": "t",
        "ל": "l",
        "מ": "m",
        "ם": "m",
        "נ": "n",
        "ן": "n",
        "ס": "s",
        "ף": "f",
        "צ": "ts",
        "ץ": "ts",
        "ק": "k",
        "ר": "r",
        "ת": "t",
    }
    return mapping.get(letter, "")


def _cap(roman: str) -> str:
    value = roman.strip()
    if not value:
        return ""
    lower = value.lower()
    for prefix in _DIGRAPH:
        if lower.startswith(prefix):
            return prefix[:1].upper() + prefix[1:] + value[len(prefix) :]
    return value[:1].upper() + value[1:]


def phonetic_from_nikud(text: str | None) -> str:
    """English spelling from pointed Hebrew. Apostrophes split vowel hits (Na'ar)."""
    value = str(text or "").strip()
    if not value or not HEBREW_RE.search(value):
        return ""
    if MAQAF_RE.search(value) and not SPLIT_KEEP_RE.search(value):
        parts = [phonetic_from_nikud(part) for part in MAQAF_RE.split(value) if part]
        return "-".join(part for part in parts if part)
    chunks: list[str] = []
    for piece in SPLIT_KEEP_RE.split(value):
        if not piece:
            continue
        if not HEBREW_RE.search(piece):
            chunks.append(piece)
            continue
        chunks.append(_cap(_word_from_nikud(piece)))
    return re.sub(r"\s+", " ", "".join(chunks)).strip(" /-|")


def _word_from_nikud(word: str) -> str:
    clusters = _clusters(word)
    out: list[str] = []
    prev_vowel = False
    first_letter = True
    for offset, (letter, marks, other) in enumerate(clusters):
        if other:
            out.append(letter)
            prev_vowel = False
            first_letter = True
            continue
        last = all(item[2] for item in clusters[offset + 1 :])
        vowel = _vowel_of(marks, sheva_na=first_letter)
        if letter == "ו" and (HOLAM in marks or HOLAM_HASER in marks):
            vowel = "o"
        elif letter == "ו" and DAGESH in marks and not any(
            mark in marks for mark in _VOWEL_MARK if mark != SHEVA
        ):
            vowel = "u"
        cons = _cons_of(letter, marks, first=first_letter, last=last)
        if letter == "י":
            if first_letter or DAGESH in marks:
                cons = "y"
            elif vowel and vowel != "i":
                cons = "y"
            elif not vowel and not first_letter:
                if prev_vowel and out[-1:] == ["i"]:
                    first_letter = False
                    continue
                if prev_vowel:
                    out.append("i")
                    first_letter = False
                    continue
                vowel = "i"
                cons = ""
        if letter in {"א", "ע"} and vowel and prev_vowel:
            out.append("'")
        if cons:
            out.append(cons)
            prev_vowel = False
        if vowel:
            if prev_vowel and not cons and out[-1:] != ["'"]:
                out.append("'")
            out.append(vowel)
            prev_vowel = True
        first_letter = False
    roman = "".join(out)
    roman = re.sub(r"'{2,}", "'", roman)
    roman = re.sub(r"(.)\1{2,}", r"\1\1", roman)
    return roman.strip("'")


def _nikud_lexicon() -> dict[str, str]:
    table = dict(_NIKUD_WORDS)
    if _LEXICON_PATH.exists():
        try:
            data = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        extra = data.get("nikud") if isinstance(data, dict) else None
        if isinstance(extra, dict):
            for key, value in extra.items():
                he = strip_nikud(str(key))
                pointed = str(value or "").strip()
                if he and pointed:
                    table[he] = pointed
    return table


def _lookup_nikud(token: str) -> str:
    table = _nikud_lexicon()
    found = table.get(token) or table.get(strip_nikud(token))
    return found or ""


def _join_pointed_prefix(prefix: str, stem: str) -> str:
    if not stem:
        return prefix
    first = stem[0]
    if prefix.endswith("הַ") and first in _BGDKPT and DAGESH not in stem[:3]:
        stem = first + DAGESH + stem[1:]
    return prefix + stem


def menukad_word(word: str) -> str:
    token = strip_nikud(word) if not has_nikud(word) else str(word or "").strip()
    raw = str(word or "").strip()
    if has_nikud(raw):
        return raw
    token = strip_nikud(raw)
    if not token:
        return ""
    if MAQAF_RE.search(token):
        parts = [menukad_word(part) for part in MAQAF_RE.split(token) if part]
        return "־".join(part for part in parts if part)
    if not HEBREW_RE.search(token):
        return token
    found = _lookup_nikud(token)
    if found:
        return found
    for prefix, pointed in _PREFIXES:
        if len(token) <= len(prefix) + 1 or not token.startswith(prefix):
            continue
        rest = token[len(prefix) :]
        stem = _lookup_nikud(rest)
        if not stem:
            stem = menukad_word(rest) if rest != token else ""
        if stem and has_nikud(stem):
            return _join_pointed_prefix(pointed, stem)
    from hebrew_phonetic_model import phonetic_word

    roman = phonetic_word(token)
    pointed = point_from_roman(token, roman)
    return pointed or token


def menukad_title(text: str | None) -> str:
    value = str(text or "").strip()
    if not value or not HEBREW_RE.search(value):
        return ""
    if has_nikud(value):
        return value
    parts: list[str] = []
    for piece in SPLIT_KEEP_RE.split(value):
        if not piece:
            continue
        if not HEBREW_RE.search(piece):
            parts.append(piece)
            continue
        parts.append(menukad_word(piece))
    return re.sub(r"\s+", " ", "".join(parts)).strip(" /-|")


def _roman_parts(roman: str) -> list[str]:
    lower = str(roman or "").strip().lower()
    parts: list[str] = []
    index = 0
    while index < len(lower):
        ch = lower[index]
        if ch in "'’׳":
            parts.append("'")
            index += 1
            continue
        pair = lower[index : index + 2]
        if pair in _DIGRAPH:
            parts.append(pair)
            index += 2
            continue
        parts.append(ch)
        index += 1
    return parts


def _letter_cons(letter: str, *, first: bool) -> tuple[str, ...]:
    if letter in {"א", "ע"}:
        return ("",)
    if letter == "ה":
        return ("h", "")
    if letter == "ו":
        return ("v", "o", "u", "")
    if letter == "י":
        return ("y", "i", "") if first else ("i", "y", "")
    if letter == "ב":
        return ("b", "v")
    if letter == "פ":
        return ("p", "f")
    if letter in {"כ", "ך"}:
        return ("k", "kh", "ch")
    if letter == "ש":
        return ("sh", "s")
    mapping = {
        "ג": ("g",),
        "ד": ("d",),
        "ז": ("z",),
        "ח": ("ch", "kh"),
        "ט": ("t",),
        "ל": ("l",),
        "מ": ("m",),
        "ם": ("m",),
        "נ": ("n",),
        "ן": ("n",),
        "ס": ("s",),
        "ף": ("f",),
        "צ": ("ts",),
        "ץ": ("ts",),
        "ק": ("k", "c"),
        "ר": ("r",),
        "ת": ("t",),
    }
    return mapping.get(letter, (letter,))


def point_from_roman(hebrew: str, roman: str) -> str:
    """Place niqqud on an unpointed word using a known English phonetic spelling."""
    letters = [ch for ch in strip_nikud(hebrew) if HEBREW_LETTER_RE.match(ch)]
    parts = _roman_parts(roman)
    if not letters:
        return strip_nikud(hebrew)
    out: list[str] = []
    cursor = 0

    def peek() -> str:
        return parts[cursor] if cursor < len(parts) else ""

    def take() -> str:
        nonlocal cursor
        if cursor >= len(parts):
            return ""
        value = parts[cursor]
        cursor += 1
        return value

    for index, letter in enumerate(letters):
        first = index == 0
        last = index == len(letters) - 1
        marks = ""
        token = peek()
        if token == "'":
            take()
            token = peek()
        options = _letter_cons(letter, first=first)
        cons = ""
        if token and token not in "aeiou" and token != "'" and token in options:
            cons = take()
            token = peek()
        elif letter in {"א", "ע"}:
            cons = ""
        elif letter == "ו" and token in {"o", "u"}:
            cons = take()
        elif letter == "י" and token == "i":
            cons = take()
        elif letter == "ה" and last and token not in "aeiou":
            cons = ""
        vowel = ""
        if peek() in "aeiou":
            vowel = take()
        elif cons in {"o", "u"} and letter == "ו":
            vowel = cons
            cons = ""
        elif cons == "i" and letter == "י":
            vowel = "i"
            cons = ""
        if letter == "ש":
            marks += SIN_DOT if cons == "s" else SHIN_DOT
        if letter in {"ב", "פ", "כ"} and cons in {"b", "p", "k"}:
            marks += DAGESH
        if letter == "ו" and vowel == "o":
            marks += HOLAM
            vowel = ""
        elif letter == "ו" and vowel == "u":
            marks += DAGESH
            vowel = ""
        elif vowel:
            mark = _ROMAN_VOWEL.get(vowel, "")
            if letter == "ה" and last and vowel == "a":
                mark = QAMATS
            if letter in {"א", "ע"} and vowel == "a" and peek() in "aeiou":
                mark = PATAH
            marks += mark
        elif not last and letter not in {"א", "ע", "ו", "י", "ה"}:
            marks += SHEVA
        out.append(letter + marks)
    extra = "".join(parts[cursor:])
    extra = re.sub(r"[aeiou']+", "", extra)
    if extra:
        return "".join(out)
    return "".join(out)
