"""Map Israeli publisher names to their public catalog sites."""

from __future__ import annotations

import re


def _norm(text: str) -> str:
    value = (text or "").casefold()
    value = re.sub(r"[^\w\u0590-\u05ff]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()

PUBLISHER_SITES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "כנרת זמורה דביר",
            "כנרת זמורה ביתן",
            "כנרת זמורה",
            "זמורה ביתן",
            "זמורה-ביתן",
            "kinneret zmora",
            "kinbooks",
        ),
        "https://www.kinbooks.co.il/",
    ),
    (("כנרת", "kinneret"), "https://www.kinbooks.co.il/"),
    (("זמורה", "דביר", "zmora", "dvir"), "https://www.kinbooks.co.il/"),
    (
        ("ידיעות ספרים", "ידיעות אחרונות", "yediot", "yedioth", "ybook"),
        "https://ybook.co.il/",
    ),
    (("ידיעות",), "https://ybook.co.il/"),
    (("מודן", "modan"), "https://www.modan.co.il/"),
    (("כתר ספרים", "keter books", "keter-books"), "https://www.keter-books.co.il/"),
    (("כתר", "keter"), "https://www.keter-books.co.il/"),
    (("עם עובד", "am oved", "am-oved"), "https://www.am-oved.co.il/"),
    (
        ("הקיבוץ המאוחד", "ספריית פועלים", "קיבוץ המאוחד", "kibutz-poalim"),
        "https://www.kibutz-poalim.co.il/",
    ),
    (("שוקן", "schocken"), "https://www.schocken.co.il/"),
    (("מטר", "matar"), "https://www.matarbooks.co.il/"),
    (("פרדס", "pardes"), "https://pardes.co.il/"),
    (("אחוזת בית", "ahuzat bayit"), "https://www.ahuzatbayit.co.il/"),
    (("רסלינג", "resling"), "https://resling.co.il/"),
    (("בבל", "babel"), "https://www.babel.co.il/"),
    (("מאגנס", "magnes"), "https://www.magnespress.co.il/"),
    (("קורן", "מגיד", "koren", "maggid"), "https://www.korenpub.com/"),
)

_STRIP_WORDS = ("הוצאת", "הוצאה לאור", "הוצאה", "לאור", "ספרים", "publishing", "books", "בעמ")
_SKIP_WORDS = {_norm(word) for word in _STRIP_WORDS}


def _haystack(publisher: str) -> str:
    tokens = [token for token in _norm(publisher).split() if token not in _SKIP_WORDS]
    return f" {' '.join(tokens)} "


def builtin_publisher_entries() -> list[tuple[str, str]]:
    by_url: dict[str, str] = {}
    for aliases, url in PUBLISHER_SITES:
        display = max(aliases, key=len)
        previous = by_url.get(url, "")
        if len(display) > len(previous):
            by_url[url] = display
    return [(name, url) for url, name in sorted(by_url.items(), key=lambda item: item[1])]


def resolve_builtin_publisher_site(publisher: str) -> str | None:
    hay = _haystack(publisher)
    if hay == "  ":
        return None
    best_url = None
    best_len = 0
    for aliases, url in PUBLISHER_SITES:
        for alias in aliases:
            needle = f" {_norm(alias)} "
            if needle in hay and len(needle) > best_len:
                best_url = url
                best_len = len(needle)
    return best_url


def publisher_name_matches_filter(name: str, needle: str) -> bool:
    """True when the publisher row should stay visible.

    Blank names stay visible so a new row is not hidden. Any other name stays
    when the needle occurs anywhere in it, not only at the start.
    """
    text = (needle or "").strip().casefold()
    if not text:
        return True
    label = (name or "").strip()
    if not label:
        return True
    return text in label.casefold()


def sort_publisher_rows(rows: list, key: str, descending: bool) -> None:
    """Sort publisher rows by name or website. Blank values stay last."""

    def value(item: object) -> tuple:
        record = item if isinstance(item, (list, tuple)) else ("", "")
        name = str(record[0] or "").strip().casefold()
        url = str(record[1] or "").strip().casefold() if len(record) > 1 else ""
        if key == "url":
            primary, secondary, blank = url, name, not url
        else:
            primary, secondary, blank = name, url, not name
        if descending:
            return (blank, tuple(-ord(ch) for ch in primary), secondary)
        return (blank, primary, secondary)

    rows.sort(key=value)


def resolve_publisher_site(publisher: str, *, include_unpreferred: bool = False) -> str | None:
    try:
        from app_config import configured_publisher_site, publisher_is_preferred

        configured = configured_publisher_site(publisher)
        if configured:
            url = configured
        else:
            url = resolve_builtin_publisher_site(publisher)
        if not url:
            return None
        if include_unpreferred or publisher_is_preferred(publisher):
            return url
        return None
    except Exception:
        pass
    return resolve_builtin_publisher_site(publisher)


def publishers_match(left: str, right: str) -> bool:
    a = _haystack(left).strip()
    b = _haystack(right).strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return a in b or b in a
