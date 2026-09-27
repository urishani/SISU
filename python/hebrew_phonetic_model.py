"""Hebrew → English phonetic spelling without an LLM.

The converter is a lexicon of real catalog words plus prefix stripping and a
letter-to-sound fallback. It transliterates; it does not translate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

NIQQUD_RE = re.compile(r"[\u05B0-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]")
HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
GERESH_RE = re.compile(r"['׳’]")
SPLIT_KEEP_RE = re.compile(r"(\s+|[“”\"():;,.!?]+)")
MAQAF_RE = re.compile(r"[־\-]")

_LEXICON_PATH = Path(__file__).with_name("hebrew_phonetic_lexicon.json")
_loaded: dict[str, str] | None = None

# Longest first. Applied only when the remainder is in the lexicon.
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("וש", "Ve"),
    ("שה", "Sheha"),
    ("כש", "Keshe"),
    ("וה", "Veha"),
    ("וב", "Uve"),
    ("ול", "Ule"),
    ("וכ", "Uke"),
    ("ומ", "Ume"),
    ("וע", "Ve"),
    ("מש", "Mishe"),
    ("מה", "Meha"),
    ("לה", "Leha"),
    ("לכ", "Leke"),
    ("בש", "Beshe"),
    ("ה", "Ha"),
    ("ו", "Ve"),
    ("ב", "Be"),
    ("כ", "Ke"),
    ("ל", "Le"),
    ("מ", "Me"),
    ("ש", "She"),
)

_GERESH = {
    "g": "j",
    "z": "zh",
    "ts": "ch",
    "c": "ch",
    "k": "ch",
}

_CONS = {
    "ב": "v",
    "ג": "g",
    "ד": "d",
    "ה": "h",
    "ז": "z",
    "ח": "ch",
    "ט": "t",
    "כ": "k",
    "ך": "kh",
    "ל": "l",
    "מ": "m",
    "ם": "m",
    "נ": "n",
    "ן": "n",
    "ס": "s",
    "פ": "f",
    "ף": "f",
    "צ": "ts",
    "ץ": "ts",
    "ק": "k",
    "ר": "r",
    "ש": "sh",
    "ת": "t",
}

_DIGRAPH = ("ch", "sh", "ts", "kh", "zh")


def normalize_hebrew(text: str | None) -> str:
    value = NIQQUD_RE.sub("", str(text or ""))
    return GERESH_RE.sub("׳", value).strip()


def lexicon() -> dict[str, str]:
    global _loaded
    if _loaded is None:
        _loaded = dict(_CORE)
        if _LEXICON_PATH.exists():
            data = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))
            words = data.get("words") if isinstance(data, dict) else data
            if isinstance(words, dict):
                for key, value in words.items():
                    he = normalize_hebrew(key)
                    en = str(value or "").strip()
                    if he and en:
                        _loaded[he] = en
    return _loaded


def phonetic_word(word: str) -> str:
    token = normalize_hebrew(word)
    if not token:
        return ""
    if MAQAF_RE.search(token):
        parts = [phonetic_word(part) for part in MAQAF_RE.split(token) if part]
        return "-".join(part for part in parts if part)
    if not HEBREW_RE.search(token):
        return token
    found = _lookup(token)
    if found:
        return found
    via_prefix = _from_prefix(token)
    if via_prefix:
        return via_prefix
    return _cap(_letters(token))


def phonetic_title(text: str | None) -> str:
    value = normalize_hebrew(text)
    if not value or not HEBREW_RE.search(value):
        return ""
    parts: list[str] = []
    for piece in SPLIT_KEEP_RE.split(value):
        if not piece:
            continue
        if not HEBREW_RE.search(piece):
            parts.append(piece)
            continue
        parts.append(phonetic_word(piece))
    return re.sub(r"\s+", " ", "".join(parts)).strip(" /-|")


def _lookup(token: str) -> str:
    table = lexicon()
    direct = table.get(token)
    if direct:
        return direct
    folded = token.replace("׳", "'")
    return table.get(folded, "")


def _from_prefix(token: str) -> str:
    table = lexicon()
    for prefix, roman in _PREFIXES:
        if len(token) <= len(prefix) + 1 or not token.startswith(prefix):
            continue
        rest = token[len(prefix) :]
        stem = table.get(rest)
        if not stem:
            continue
        return _join_prefix(roman, stem)
    if token.startswith("ה") and len(token) >= 4:
        rest = token[len("ה") :]
        if rest and HEBREW_RE.search(rest):
            return _join_prefix("Ha", _cap(_letters(rest)))
    return ""


def _join_prefix(prefix: str, stem: str) -> str:
    body = stem[:1].lower() + stem[1:] if stem else ""
    if body[:1] in "aeiouAEIOU" or body.startswith("'"):
        joined = prefix + "'" + body.lstrip("'")
    else:
        joined = prefix + body
    return _cap(joined)


def _letters(word: str) -> str:
    letters = list(word)
    chunks: list[tuple[str, bool]] = []
    index = 0
    while index < len(letters):
        ch = letters[index]
        nxt = letters[index + 1] if index + 1 < len(letters) else ""
        geresh = nxt == "׳"
        if ch in {"א", "ע"}:
            if nxt in {"ו", "י"}:
                index += 1
                continue
            chunks.append(("a", True))
            index += 1
            continue
        if ch == "ו" and nxt == "ו":
            chunks.append(("v", False))
            index += 2
            continue
        if ch == "י" and nxt == "י":
            chunks.append(("y", False))
            index += 2
            continue
        if ch == "ו":
            if index == 0:
                chunks.append(("v", False))
            else:
                chunks.append(("o", True))
            index += 1
            continue
        if ch == "י":
            if index == 0:
                chunks.append(("y", False))
            else:
                chunks.append(("i", True))
            index += 1
            continue
        if ch == "ה" and index == len(letters) - 1:
            if not any(vowel for _text, vowel in chunks):
                chunks.append(("a", True))
            index += 1
            continue
        if ch == "ב":
            sound = "b" if index == 0 else "v"
            chunks.append((sound, False))
            index += 1
            continue
        if ch == "פ":
            sound = "p" if index == 0 else "f"
            chunks.append((sound, False))
            index += 1
            continue
        mapped = _CONS.get(ch, ch if not HEBREW_RE.search(ch) else "")
        if geresh and mapped in _GERESH:
            mapped = _GERESH[mapped]
            index += 2
            if mapped:
                chunks.append((mapped, False))
            continue
        if mapped:
            vowel = mapped in {"a", "e", "i", "o", "u"}
            chunks.append((mapped, vowel))
        index += 1
    out: list[str] = []
    prev_vowel = True
    for text, vowel in chunks:
        if not text:
            continue
        if not vowel and not prev_vowel:
            out.append("e")
        out.append(text)
        prev_vowel = vowel or text[-1:] in "aeiou"
    roman = re.sub(r"(.)\1{2,}", r"\1\1", "".join(out))
    return roman.strip("'")


def _cap(roman: str) -> str:
    value = roman.strip()
    if not value:
        return ""
    lower = value.lower()
    for prefix in _DIGRAPH:
        if lower.startswith(prefix):
            return prefix[:1].upper() + prefix[1:] + value[len(prefix) :]
    return value[:1].upper() + value[1:]


# High-frequency Hebrew and stems so prefix stripping works on new titles.
_CORE: dict[str, str] = {
    "א": "A",
    "אב": "Av",
    "אבא": "Aba",
    "אביב": "Aviv",
    "אבות": "Avot",
    "אגדה": "Agada",
    "אגדת": "Agadat",
    "אדם": "Adam",
    "אהבה": "Ahava",
    "אהוב": "Ahuv",
    "אהובי": "Ahuvi",
    "או": "O",
    "אור": "Or",
    "אז": "Az",
    "אחד": "Echad",
    "אחות": "Achot",
    "אחרון": "Acharon",
    "אחרונה": "Acharona",
    "אחרי": "Acharei",
    "אחרים": "Acherim",
    "אחרת": "Acheret",
    "אחת": "Achat",
    "אי": "I",
    "אין": "Ein",
    "איש": "Ish",
    "אישי": "Ishi",
    "אישה": "Isha",
    "איך": "Eikh",
    "אימא": "Ima",
    "אם": "Im",
    "אמא": "Ima",
    "אמונה": "Emuna",
    "אמת": "Emet",
    "אני": "Ani",
    "אנחנו": "Anachnu",
    "אנשים": "Anashim",
    "אנשי": "Anshei",
    "אפשר": "Efshar",
    "אף": "Af",
    "ארץ": "Eretz",
    "אש": "Esh",
    "את": "Et",
    "אתה": "Ata",
    "ב": "B",
    "בא": "Ba",
    "בית": "Bayit",
    "בן": "Ben",
    "בנים": "Banim",
    "בנות": "Banot",
    "בוקר": "Boker",
    "בת": "Bat",
    "ג": "G",
    "גדול": "Gadol",
    "גדולה": "Gdola",
    "גם": "Gam",
    "גן": "Gan",
    "ד": "D",
    "דבר": "Davar",
    "דברים": "Dvarim",
    "דם": "Dam",
    "דרך": "Derech",
    "ה": "Ha",
    "הוא": "Hu",
    "היא": "Hi",
    "היה": "Haya",
    "הם": "Hem",
    "ו": "Ve",
    "זאת": "Zot",
    "זה": "Ze",
    "זהב": "Zahav",
    "זהות": "Zehut",
    "זמן": "Zman",
    "חבר": "Chaver",
    "חברה": "Chevra",
    "חברים": "Chaverim",
    "חברות": "Chaverot",
    "חיים": "Chayim",
    "חכם": "Chacham",
    "חכמה": "Chochma",
    "חלק": "Chelek",
    "חתול": "Chatul",
    "טוב": "Tov",
    "טובה": "Tova",
    "יד": "Yad",
    "יהודי": "Yehudi",
    "יהודים": "Yehudim",
    "יהודית": "Yehudit",
    "יום": "Yom",
    "יומן": "Yoman",
    "ימי": "Yemei",
    "ימים": "Yamim",
    "ילד": "Yeled",
    "ילדה": "Yalda",
    "ילדים": "Yeladim",
    "ים": "Yam",
    "יש": "Yesh",
    "ישראל": "Yisrael",
    "ירושלים": "Yerushalayim",
    "כ": "K",
    "כאן": "Kan",
    "כוח": "Koach",
    "כוכב": "Kochav",
    "כולם": "Kulam",
    "כי": "Ki",
    "ככה": "Kacha",
    "כל": "Kol",
    "כלב": "Kelev",
    "כמו": "Kmo",
    "כמה": "Kama",
    "כן": "Ken",
    "כסף": "Kesef",
    "כתב": "Katav",
    "ל": "L",
    "לא": "Lo",
    "לב": "Lev",
    "לו": "Lo",
    "לי": "Li",
    "לילה": "Laila",
    "למה": "Lama",
    "מ": "M",
    "מדריך": "Madrikh",
    "מים": "Mayim",
    "מלך": "Melech",
    "מלחמה": "Milchama",
    "מסע": "Masa",
    "מקום": "Makom",
    "משפחה": "Mishpacha",
    "משחק": "Mischak",
    "מת": "Met",
    "מה": "Ma",
    "מי": "Mi",
    "נער": "Na'ar",
    "נערה": "Na'ara",
    "נפש": "Nefesh",
    "נשים": "Nashim",
    "סוד": "Sod",
    "סוף": "Sof",
    "ספר": "Sefer",
    "ספרים": "Sfarim",
    "ספריה": "Sifriya",
    "ספרייה": "Sifriya",
    "סופר": "Sofer",
    "עולם": "Olam",
    "עוד": "Od",
    "עד": "Ad",
    "עידן": "Idan",
    "על": "Al",
    "עיר": "Ir",
    "עץ": "Etz",
    "עין": "Ayin",
    "עיניים": "Einayim",
    "עם": "Im",
    "עצמי": "Atzmi",
    "פעם": "Pa'am",
    "פרק": "Perek",
    "צל": "Tzel",
    "קול": "Kol",
    "קטן": "Katan",
    "קסם": "Kesem",
    "ראש": "Rosh",
    "רב": "Rav",
    "רוח": "Ruach",
    "רק": "Rak",
    "ש": "She",
    "של": "Shel",
    "שלו": "Shelo",
    "שלי": "Sheli",
    "שלך": "Shelcha",
    "שלנו": "Shelanu",
    "שם": "Sham",
    "שמש": "Shemesh",
    "שנה": "Shana",
    "שיר": "Shir",
    "שירים": "Shirim",
    "שלום": "Shalom",
    "שקט": "Sheket",
    "תורה": "Tora",
    "תקווה": "Tikva",
    "הציל": "Hetzil",
    "טעויות": "Ta'uyot",
    "אלמנות": "Almanot",
    "זכוכית": "Zchuchit",
    "אנטומיה": "Anatomia",
    "גיהנום": "Gehinom",
    "שיגעון": "Shigaon",
    "ניצוץ": "Nitzotz",
    "מוטיבציה": "Motivatsya",
    "השראה": "Hashra'a",
    "אנקדוטות": "Anekdotot",
    "חת": "Chat",
    "אומץ": "Ometz",
    "נכד": "Neched",
    "חצר": "Chatzer",
    "נדרים": "Nedarim",
    "סליחה": "Slicha",
    "נתן": "Natan",
    "חנוך": "Chanokh",
    "קהילה": "Kehila",
    "רחל": "Rachel",
    "רבין": "Rabin",
    "בריאות": "Bri'ut",
    "אגדות": "Agadot",
    "חטופים": "Chatufim",
    "כותב": "Kotev",
    "משימה": "Mesima",
    "חלב": "Chalav",
    "שימוש": "Shimush",
    "תובנות": "Tovanot",
    "לוי": "Levi",
    "אוויר": "Avir",
    "הרים": "Harim",
    "חודשים": "Chodashim",
    "אבל": "Aval",
    "גשר": "Gesher",
    "אסיה": "Asya",
    "חלום": "Chalom",
    "יחידה": "Yechida",
    "אהבת": "Ahavat",
    "אמריקה": "America",
    "שמע": "Shma",
    "סיבות": "Sibot",
    "צוות": "Tzevet",
    "השפעה": "Hashpa'a",
    "יהלום": "Yahalom",
    "דבש": "Dvash",
    "אלף": "Elef",
    "כולנו": "Kulanu",
    "חנות": "Chanut",
    "כנפיים": "Knafayim",
    "אסתר": "Ester",
    "ישו": "Yeshu",
    "תה": "Te",
    "עיצוב": "Itzuv",
    "נשיא": "Nasi",
    "קרב": "Krav",
    "אדמה": "Adama",
    "תלמיד": "Talmid",
    "חלום": "Chalom",
    "שועל": "Shu'al",
    "חושך": "Choshech",
    "יונה": "Yona",
    "כלה": "Kala",
    "משפטים": "Mishpatim",
    "תיאטרון": "Te'atron",
    "בעיות": "Ba'ayot",
    "כרמל": "Carmel",
    "אבן": "Even",
    "מלחמות": "Milchamot",
    "שעות": "Sha'ot",
    "שלושה": "Shlosha",
    "רוסיה": "Russia",
    "חמאס": "Hamas",
    "רומא": "Roma",
    "כומר": "Komer",
    "טרור": "Terror",
    "מהגרים": "Mehagrim",
    "צהוב": "Tsahov",
    "פרויד": "Freud",
    "משחקים": "Mischakim",
    "בינה": "Bina",
    "אלימות": "Alimut",
    "חוק": "Chok",
    "חיה": "Chaya",
    "ארגון": "Irgun",
    "קצת": "Ktzat",
    "מחשב": "Machshev",
    "ביטחון": "Bitachon",
    "כוחות": "Kochot",
    "עצב": "Etsev",
    "אוסטריה": "Austria",
    "בבקשה": "Bevakasha",
    "שולחן": "Shulchan",
    "עורך": "Orech",
    "רוצח": "Rotzeach",
    "מיתוס": "Mitos",
    "ברכה": "Bracha",
    "כוס": "Kos",
    "פסיכולוגיה": "Psichologia",
    "אנרגיה": "Energia",
    "חום": "Chom",
    "גל": "Gal",
    "עיני": "Einai",
    "טיפול": "Tipul",
}
