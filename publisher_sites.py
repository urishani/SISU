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


def _haystack(publisher: str) -> str:
    skip = {_norm(word) for word in _STRIP_WORDS}
    tokens = [token for token in _norm(publisher).split() if token not in skip]
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


def resolve_publisher_site(publisher: str) -> str | None:
    try:
        from app_config import configured_publisher_site

        configured = configured_publisher_site(publisher)
        if configured:
            return configured
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
