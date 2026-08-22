"""Crawl a bookstore website and extract catalog fields for a given year."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, FeatureNotFound, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ProgressFn = Callable[[str], None]


def _choose_html_parser() -> str:
    for name in ("lxml", "html.parser"):
        try:
            BeautifulSoup("<p></p>", name)
            return name
        except Exception:
            continue
    return "html.parser"


HTML_PARSER = _choose_html_parser()


def parse_html(markup: str) -> BeautifulSoup:
    markup = markup or ""
    try:
        return BeautifulSoup(markup, HTML_PARSER)
    except FeatureNotFound:
        return BeautifulSoup(markup, "html.parser")

HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
ISBN_RE = re.compile(r"(?:97[89][-\s]?)?(?:\d[-\s]?){9,12}[\dXx]")
PAGES_RE = re.compile(r"(\d+)\s*(?:עמודים|עמ['׳’]?|pages)\b", re.I)
PRICE_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:₪|ש[\"״]?ח|ils|nis)?", re.I)
TRAILING_CODE_RE = re.compile(r"(\d{10,13})$")
SKIP_PATH_BITS = (
    "/cart",
    "/checkout",
    "/login",
    "/account",
    "/wishlist",
    "/customer",
    "/mailto:",
    "/search/suggest",
)
PRODUCT_PATH_HINTS = (
    "/מוצרים/",
    "/product/",
    "/products/",
    "/catalog/product/",
    "/p/",
    "/item/",
    "/book/",
    "/ספר/",
    "/ספרים/",
    "/product-page/",
)
PRODUCT_HREF_RE = re.compile(
    r"(?:https?://[^/\"']+)?(/(?:product|products|product-page|מוצרים|ספר|book)/[^\"'\s<>#]+)",
    re.I,
)
FILLABLE_FIELDS = (
    "title",
    "author",
    "publisher",
    "year",
    "pages",
    "isbn",
    "danacode",
    "upc",
    "cover_type",
    "weight_kg",
    "height_cm",
    "width_cm",
    "thickness_cm",
    "price_ils",
    "description",
    "cover_image_url",
    "back_image_url",
)
LABEL_MAP: dict[str, tuple[str, ...]] = {
    "publisher": ("publisher", "הוצאה", "הוצאה לאור", "מוציא לאור", "הוצאת", "manufacturer"),
    "author": (
        "author",
        "author(s)",
        "מחבר",
        "מחבר/ת",
        "מחברת",
        "סופר",
        "סופרת",
        "מאת",
        "שם מחבר",
        "שם המחבר",
        "written by",
    ),
    "year": (
        "copyright year",
        "date published",
        "published",
        "publication year",
        "שנת הוצאה",
        "שנת הוצאה לאור",
        "שנת פרסום",
        "שנה",
        "תאריך הוצאה",
        "ת. הוצאה",
        "year",
        "release date",
    ),
    "pages": ("pages", "number of pages", "עמודים", "מספר עמודים", "מס' עמודים", "page count"),
    "isbn": ("isbn", "isbn-13", "isbn13", 'מסת"ב', "מסתב", "מסת״ב"),
    "danacode": ("danacode", "דנאקוד", "דאנאקוד", "דאנא קוד", "דנא קוד", "דנא-קוד"),
    "upc": ("upc", "ean", "barcode", "ברקוד", "gtin"),
    "cover": ("cover", "cover type", "format", "book format", "כריכה", "סוג כריכה"),
    "weight": ("weight", "משקל"),
    "height": ("height", "גובה", "hight"),
    "width": ("width", "רוחב"),
    "thickness": ("thickness", "depth", "עובי"),
    "dimensions": ("dimensions", "size", "מידות", "גודל", "פורמט"),
    "price": ("price", "מחיר", "מחיר באתר", "israeli price"),
    "description": ("description", "תקציר", "תיאור", "גב הספר", "about"),
    "title": ("title", "name", "שם", "כותרת", "שם ספר", "שם הספר"),
}
SITE_DISPLAY_NAMES = {
    "booknet.co.il": "Booknet",
    "e-vrit.co.il": "e-vrit",
    "evrit.co.il": "e-vrit",
    "kinbooks.co.il": "Kinneret Zmora",
    "ybook.co.il": "Yediot",
    "modan.co.il": "Modan",
    "keter-books.co.il": "Keter",
    "am-oved.co.il": "Am Oved",
    "kibutz-poalim.co.il": "Hakibbutz Hameuchad",
    "schocken.co.il": "Schocken",
    "matarbooks.co.il": "Matar",
    "pardes.co.il": "Pardes",
    "ahuzatbayit.co.il": "Ahuzat Bayit",
    "resling.co.il": "Resling",
    "babel.co.il": "Babel",
    "magnespress.co.il": "Magnes",
    "korenpub.com": "Koren",
}


def site_host(url: str) -> str:
    host = urlparse(url or "").netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host


def site_display_name(url: str) -> str:
    host = site_host(url)
    return SITE_DISPLAY_NAMES.get(host, host or "site")


@dataclass
class Book:
    url: str
    title: str = ""
    author: str = ""
    publisher: str = ""
    year: str = ""
    pages: str = ""
    isbn: str = ""
    danacode: str = ""
    upc: str = ""
    cover_type: str = ""
    weight_kg: str = ""
    height_cm: str = ""
    width_cm: str = ""
    thickness_cm: str = ""
    price_ils: str = ""
    description: str = ""
    cover_image_url: str = ""
    back_image_url: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    scanner_id: str = ""
    scan_status: str = ""
    scan_message: str = ""
    approved: bool = False
    excel_passed: bool = False
    final: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Book":
        allowed = {item.name for item in fields(cls)}
        kwargs = {key: value for key, value in data.items() if key in allowed and key != "extra"}
        for flag in ("approved", "excel_passed", "final"):
            if flag in kwargs:
                kwargs[flag] = bool(kwargs[flag])
        book = cls(**kwargs)
        extra = data.get("extra") or {}
        if isinstance(extra, dict):
            book.extra = {str(key): str(value) for key, value in extra.items()}
        return book

    def key(self) -> str:
        return self.scanner_id or self.isbn or self.danacode or self.url or self.display_title()

    def display_title(self) -> str:
        return self.title or self.url

    def identity_code(self) -> str:
        return self.isbn or self.danacode or self.upc or ""

    def missing_fields(self) -> list[str]:
        return [name for name in FILLABLE_FIELDS if not str(getattr(self, name, "") or "").strip()]

    def append_scan_log(self, line: str) -> None:
        text = str(line or "").strip()
        if not text:
            return
        existing = (self.scan_message or "").strip()
        if existing:
            if text in existing.splitlines():
                return
            self.scan_message = existing + "\n" + text
        else:
            self.scan_message = text

    def mark_scan_failed(self, message: str) -> None:
        self.scan_status = "failed"
        self.append_scan_log(message)

    def refresh_scan_status(self) -> None:
        if not (self.title or "").strip():
            self.scan_status = "failed"
            if not (self.scan_message or "").strip():
                self.scan_message = "Opened the page but could not read a title."
            return
        if not self.missing_fields():
            self.scan_status = "fully scanned"
            if "fully scanned" not in (self.scan_message or "").casefold():
                self.append_scan_log("Fully scanned: all fillable fields are set.")
            return
        self.scan_status = "successful"

    def workflow_label(self) -> str:
        if self.final:
            return "Done"
        if self.excel_passed:
            return "Mark final"
        if self.scan_status == "failed" and not (self.title or "").strip():
            return "—"
        return "Approve"

    def status_label(self) -> str:
        if self.final:
            return "Final"
        if self.approved:
            return "Approved"
        return self.scan_status or "successful"

    def _load_map(self, key: str) -> dict[str, str]:
        raw = self.extra.get(key) or ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(name): str(value) for name, value in data.items() if name and value}

    def _save_map(self, key: str, data: dict[str, str]) -> None:
        self.extra[key] = json.dumps(data, ensure_ascii=False)

    def record_site_page(self, url: str) -> None:
        page = (url or "").strip()
        if not page:
            return
        host = site_host(page)
        if not host:
            return
        pages = self._load_map("site_pages")
        pages[host] = page
        self._save_map("site_pages", pages)
        sources = self.extra.get("sources", "")
        if page not in sources.split("|"):
            self.extra["sources"] = (sources + "|" + page).strip("|")

    def record_field_source(self, name: str, url: str) -> None:
        page = (url or "").strip()
        if not name or not page:
            return
        sources = self._load_map("field_sources")
        if name not in sources:
            sources[name] = page
        self._save_map("field_sources", sources)

    def mark_origin_fields(self) -> None:
        self.record_site_page(self.url)
        for name in FILLABLE_FIELDS:
            if str(getattr(self, name, "") or "").strip():
                self.record_field_source(name, self.url)

    def field_source_url(self, name: str) -> str:
        return self._load_map("field_sources").get(name, "")

    def source_display(self, name: str) -> str:
        url = self.field_source_url(name)
        if not url or site_host(url) == site_host(self.url):
            return ""
        return site_display_name(url)

    def is_external_source(self, name: str) -> bool:
        return bool(self.source_display(name))

    def site_pages(self) -> dict[str, str]:
        pages = self._load_map("site_pages")
        if self.url:
            pages.setdefault(site_host(self.url), self.url)
        for value in (self.extra.get("sources") or "").split("|"):
            page = value.strip()
            if page:
                pages.setdefault(site_host(page), page)
        publisher_page = (self.extra.get("publisher_page") or "").strip()
        if publisher_page:
            pages.setdefault(site_host(publisher_page), publisher_page)
        return {host: page for host, page in pages.items() if host and page}

    def has_page_on(self, site_url: str) -> bool:
        host = site_host(site_url)
        if not host:
            return False
        return host in self.site_pages()

    def merge_missing(self, other: "Book") -> list[str]:
        filled: list[str] = []
        filled.extend(remember_danacode(self, other.danacode, other.field_source_url("danacode") or other.url))
        for name in FILLABLE_FIELDS:
            if name == "danacode":
                continue
            current = str(getattr(self, name, "") or "").strip()
            incoming = str(getattr(other, name, "") or "").strip()
            if name == "price_ils" and incoming:
                try:
                    better = not current or float(format_price(incoming)) > float(format_price(current) or 0)
                except ValueError:
                    better = not current
                if better:
                    self.price_ils = incoming
                    filled.append("price_ils")
                    self.record_field_source("price_ils", other.field_source_url("price_ils") or other.url)
                continue
            if not current and incoming:
                setattr(self, name, incoming)
                filled.append(name)
                self.record_field_source(name, other.field_source_url(name) or other.url)
        if other.url:
            self.record_site_page(other.url)
        other_captured = other.captured_fields()
        captured = self.captured_fields()
        for name, incoming in other_captured.items():
            if name == "language":
                from field_map import isolate_language

                incoming = isolate_language(incoming)
            if incoming and not captured.get(name):
                captured[name] = incoming
                filled.append(name)
                self.record_field_source(name, other.field_source_url(name) or other.url)
        if captured:
            self._save_map("captured", captured)
        if other.extra.get("found_fields"):
            self.extra["publisher_found"] = other.extra["found_fields"]
        if other.extra.get("page_fields"):
            self.extra["page_fields"] = other.extra["page_fields"]
        return list(dict.fromkeys(filled))

    def captured_fields(self) -> dict[str, str]:
        data = self._load_map("captured")
        raw = data.get("language") or ""
        if raw:
            from field_map import isolate_language

            cleaned = isolate_language(raw)
            if cleaned != raw:
                if cleaned:
                    data["language"] = cleaned
                else:
                    data.pop("language", None)
                self._save_map("captured", data)
        return data

    def set_captured(self, name: str, value: str) -> None:
        data = self.captured_fields()
        text = str(value or "").strip()
        if name == "language":
            from field_map import isolate_language

            text = isolate_language(text)
        if name and text and not data.get(name):
            data[name] = text
            self._save_map("captured", data)

    def unmatched_page_fields(self) -> list[tuple[str, str]]:
        return [(item["label"], item["value"]) for item in self._load_field_rows("page_fields")]

    def publisher_found_fields(self) -> list[dict[str, str]]:
        rows = self._load_field_rows("publisher_found")
        if rows:
            return rows
        return self._load_field_rows("found_fields")

    def danacode_short(self) -> str:
        short = (
            self.captured_fields().get("cat_number")
            or self.extra.get("danacode_short")
            or ""
        ).strip()
        digits = re.sub(r"\D", "", short)
        long_code = re.sub(r"\D", "", self.danacode or "")
        if digits and digits != long_code:
            return digits
        return ""

    def _load_field_rows(self, key: str) -> list[dict[str, str]]:
        raw = self.extra.get(key) or ""
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        rows: list[dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            field = str(item.get("field") or "").strip()
            if label and value:
                rows.append({"label": label, "value": value, "field": field})
        return rows

    def mark_publisher_lookup(self, site: str, page: str = "", filled: list[str] | None = None, note: str = "") -> None:
        if site:
            self.extra["publisher_site"] = site
        if page:
            self.extra["publisher_page"] = page
        self.extra["new_fields"] = ",".join(filled or [])
        self.extra["lookup_note"] = note

    def matches_year(self, year: str | None, include_unknown: bool = False) -> bool:
        if not year:
            return True
        current = str(self.year).strip()
        if not current:
            return include_unknown
        return current == str(year).strip()

    def to_excel_fields(self) -> dict[str, str]:
        title_he, title_en = split_lang(self.title)
        author_he, author_en = split_lang(self.author)
        desc_he, desc_en = split_lang(self.description)
        captured = self.captured_fields()
        if captured.get("description_en"):
            desc_en = captured.get("description_en") or desc_en
        fields = {
            "publisher": clean(self.publisher),
            "author_en": author_en,
            "title_en": title_en,
            "title_he": title_he,
            "author_he": author_he,
            "upc": clean(self.upc),
            "danacode": clean(self.danacode),
            "cat_number": self.danacode_short() or captured.get("cat_number", ""),
            "isbn": clean(self.isbn),
            "year": clean(self.year),
            "pages": clean(self.pages),
            "cover_type": clean(self.cover_type),
            "weight_kg": clean(self.weight_kg),
            "height_cm": clean(self.height_cm),
            "width_cm": clean(self.width_cm),
            "thickness_cm": clean(self.thickness_cm),
            "price_ils": format_price(self.price_ils),
            "description_he": desc_he,
            "description_en": desc_en,
            "cover_image_url": clean(self.cover_image_url),
            "back_image_url": clean(self.back_image_url),
            "scanner_id": clean(self.scanner_id),
            "url": self.url,
        }
        if captured.get("author_en"):
            author_en = captured.get("author_en") or author_en
        if captured.get("author_he"):
            author_he = captured.get("author_he") or author_he
        fields["author_en"] = format_person_name(author_en)
        fields["author_he"] = format_person_name(author_he)
        if captured.get("translated"):
            fields["translated"] = format_person_name(captured["translated"]) if _looks_like_person_name(
                captured["translated"]
            ) else captured["translated"]
        for name, value in captured.items():
            if name not in fields or not fields[name]:
                fields[name] = value
        return fields


class CrawlCancelled(Exception):
    pass


@dataclass
class CrawlReport:
    listing_pages: int = 0
    listing_failed: int = 0
    product_links: int = 0
    product_cached: int = 0
    product_fetched: int = 0
    product_failed: int = 0
    matched: int = 0
    skipped_year: int = 0
    enriched: int = 0
    from_cache: bool = False
    cancelled: bool = False

    def summary(self) -> str:
        if self.from_cache:
            return f"From cache: {self.matched} book(s) loaded. No pages fetched."
        parts = [
            f"Listing pages: {self.listing_pages}",
            f"book links: {self.product_links}",
            f"from cache: {self.product_cached}",
            f"downloaded: {self.product_fetched}",
            f"failed: {self.listing_failed + self.product_failed}",
            f"matched: {self.matched}",
        ]
        if self.skipped_year:
            parts.append(f"wrong year: {self.skipped_year}")
        if self.enriched:
            parts.append(f"filled from other sites: {self.enriched}")
        if self.cancelled:
            parts.append("stopped early")
        return "Search summary — " + ", ".join(parts) + "."


def has_hebrew(text: str | None) -> bool:
    return bool(HEBREW_RE.search(text or ""))


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text


def split_lang(text: str | None) -> tuple[str, str]:
    text = clean(text)
    if not text:
        return "", ""
    if has_hebrew(text):
        return text, ""
    return "", text


_NAME_PARTICLES = {
    "da",
    "de",
    "del",
    "della",
    "der",
    "di",
    "el",
    "la",
    "le",
    "van",
    "von",
    "bin",
    "ibn",
    "al",
    "ben",
    "mac",
    "mc",
    "בן",
    "בת",
    "אל",
    "אבן",
    "דה",
    "די",
    "ואן",
    "פון",
}


def _looks_like_person_name(text: str | None) -> bool:
    value = clean(text)
    if not value:
        return False
    compact = value.casefold()
    if compact in {"yes", "no", "true", "false", "כן", "לא", "y", "n"}:
        return False
    if re.fullmatch(r"[\d\s\-]+", value):
        return False
    words = value.split()
    return 1 <= len(words) <= 8


def _split_people(text: str) -> list[str]:
    value = clean(text)
    if not value:
        return []
    if re.search(r"\s+(?:and|&|/|ו)\s+", value, re.I) or ";" in value:
        parts = re.split(r"\s+(?:and|&|/|ו)\s+|;", value, flags=re.I)
        return [clean(part) for part in parts if clean(part)]
    if "," in value:
        left, right = value.split(",", 1)
        if len(left.split()) >= 2 and len(right.split()) >= 2:
            return [clean(left), clean(right)]
    return [value]


def _format_one_person(name: str) -> str:
    value = clean(name)
    if not value:
        return ""
    if "," in value:
        family, given = (part.strip() for part in value.split(",", 1))
        if family and given:
            return f"{family}, {given}"
        return value
    tokens = value.split()
    if len(tokens) == 1:
        return value
    if len(tokens) >= 3 and tokens[-2].casefold() in _NAME_PARTICLES:
        family = f"{tokens[-2]} {tokens[-1]}"
        given = " ".join(tokens[:-2])
        return f"{family}, {given}" if given else family
    family = tokens[-1]
    given = " ".join(tokens[:-1])
    return f"{family}, {given}" if given else family


def format_person_name(text: str | None) -> str:
    people = [_format_one_person(part) for part in _split_people(text or "")]
    return "; ".join(part for part in people if part)


def normalize_name(text: str | None) -> str:
    value = (text or "").casefold()
    value = value.replace("״", '"').replace("׳", "'")
    value = re.sub(r"[^\w\u0590-\u05ff]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def identity_keys(book: Book) -> set[str]:
    keys: set[str] = set()
    if book.isbn:
        keys.add(f"isbn:{book.isbn}")
    if book.danacode:
        keys.add(f"dana:{book.danacode}")
    title = normalize_name(book.title)
    author = normalize_name(book.author)
    if title:
        keys.add(f"title:{title}")
        if author:
            keys.add(f"ta:{title}|{author}")
    return keys


def books_match(left: Book, right: Book) -> bool:
    left_keys = identity_keys(left)
    right_keys = identity_keys(right)
    if left_keys & right_keys:
        return True
    left_title = normalize_name(left.title)
    right_title = normalize_name(right.title)
    if left_title and right_title and (left_title in right_title or right_title in left_title):
        left_author = normalize_name(left.author)
        right_author = normalize_name(right.author)
        if not left_author or not right_author:
            return len(left_title) >= 8 and len(right_title) >= 8
        return left_author in right_author or right_author in left_author
    return False


def merge_catalog(primary: list[Book], extras: list[Book]) -> int:
    filled = 0
    for extra in extras:
        for book in primary:
            if books_match(book, extra):
                if book.merge_missing(extra):
                    filled += 1
                break
    return filled


def extract_year(text: str | None) -> str:
    if not text:
        return ""
    match = YEAR_RE.search(str(text))
    return match.group(0) if match else ""


def extract_codes(raw: str) -> list[tuple[str, str]]:
    text = unquote(raw or "")
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, code: str) -> None:
        if kind and code and code not in seen:
            seen.add(code)
            results.append((kind, code))

    compact = re.sub(r"[^0-9Xx]", "", text)
    for match in re.finditer(r"97[89]\d{10}", compact):
        add("isbn", match.group(0))

    path = urlparse(text).path if "://" in text else text
    trailing = re.search(r"-(\d{10,13})/?$", path)
    if trailing:
        kind, code = classify_code(trailing.group(1))
        add(kind, code)

    if re.fullmatch(r"[\dXx][\dXx\- ]{8,16}[\dXx]", text.strip()):
        kind, code = classify_code(text)
        add(kind, code)
    return results


def classify_code(raw: str) -> tuple[str, str]:
    digits = re.sub(r"[^0-9Xx]", "", raw or "")
    if not digits:
        return "", ""
    if digits.upper().startswith(("978", "979")) and len(digits) == 13:
        return "isbn", digits
    if len(digits) == 10 and digits[:9].isdigit():
        return "isbn", digits.upper()
    if 11 <= len(digits) <= 13:
        return "danacode", digits
    if len(digits) == 12:
        return "upc", digits
    return "", digits


def apply_identifier(book: Book, raw: str) -> None:
    for kind, code in extract_codes(raw):
        if kind == "isbn" and not book.isbn:
            book.isbn = code
        elif kind == "danacode":
            remember_danacode(book, code)
        elif kind == "upc" and not book.upc:
            book.upc = code


def remember_danacode(book: Book, raw: str | None, source_url: str = "") -> list[str]:
    """Keep the longer trade DanaCode and a shorter publisher/catalog code when both appear."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) < 4:
        return []
    long_code = re.sub(r"\D", "", book.danacode or "")
    short_code = re.sub(r"\D", "", book.danacode_short() or book.extra.get("danacode_short") or "")
    url = (source_url or book.url or "").strip()
    filled: list[str] = []

    def set_short(code: str) -> None:
        if not code or code == re.sub(r"\D", "", book.danacode or ""):
            return
        current_short = re.sub(r"\D", "", book.danacode_short() or book.extra.get("danacode_short") or "")
        if current_short == code:
            return
        book.extra["danacode_short"] = code
        book.set_captured("cat_number", code)
        if url:
            book.record_field_source("cat_number", url)
        filled.append("cat_number")

    if not long_code:
        book.danacode = digits
        if url:
            book.record_field_source("danacode", url)
        filled.append("danacode")
        return filled
    if digits == long_code:
        return filled
    if len(digits) > len(long_code):
        set_short(long_code)
        book.danacode = digits
        if url:
            book.record_field_source("danacode", url)
        filled.append("danacode")
        return filled
    set_short(digits)
    return filled


def map_cover(text: str | None) -> str:
    from field_map import cover_code

    return cover_code(text)


def parse_price(text: str | None) -> str:
    if text is None:
        return ""
    match = PRICE_RE.search(str(text).replace(",", ""))
    if not match:
        return ""
    return format_price(match.group(1)) or match.group(1)


def format_price(text: str | None) -> str:
    raw = str(text or "").strip().replace(",", "")
    if not raw:
        return ""
    try:
        return f"{float(raw):.2f}"
    except ValueError:
        return str(text or "").strip()


def prefer_catalog_price(book: Book, soup: BeautifulSoup) -> None:
    """Use the list/catalog price when a sale price is shown struck through."""
    catalog = ""
    for block in soup.select(".price--on-sale .price__sale, .price__sale"):
        struck = block.select_one("s, del, strike")
        if struck:
            catalog = parse_price(struck.get_text(" ", strip=True))
            if catalog:
                break
    if not catalog:
        for node in soup.find_all(string=re.compile(r"מחיר\s*קטלוגי|compare[-_ ]?at", re.I)):
            parent = getattr(node, "parent", None)
            if parent is None:
                continue
            container = parent.parent if parent.parent else parent
            catalog = parse_price(container.get_text(" ", strip=True))
            if catalog:
                break
    if catalog:
        book.price_ils = catalog


def parse_pages(text: str | None) -> str:
    if not text:
        return ""
    match = PAGES_RE.search(text)
    return match.group(1) if match else ""


def parse_weight_kg(text: str | None) -> str:
    if not text:
        return ""
    raw = text.lower().replace(",", ".")
    number = re.search(r"(\d+(?:\.\d+)?)", raw)
    if not number:
        return ""
    amount = float(number.group(1))
    if "oz" in raw:
        amount *= 0.0283495
    elif "lb" in raw or "lbs" in raw:
        amount *= 0.453592
    elif "גרם" in raw or re.search(r"\bg\b", raw):
        amount /= 1000.0
    return f"{amount:.3f}".rstrip("0").rstrip(".")


def parse_cm_triplet(text: str | None) -> tuple[str, str, str]:
    if not text:
        return "", "", ""
    nums = re.findall(r"(\d+(?:\.\d+)?)", text.replace(",", "."))
    if len(nums) >= 3:
        return nums[0], nums[1], nums[2]
    if len(nums) == 2:
        return nums[0], nums[1], ""
    if len(nums) == 1:
        return nums[0], "", ""
    return "", "", ""


def json_ld_objects(html: str) -> list[dict]:
    soup = parse_html(html)
    found: list[dict] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                found.append(item)
                for value in item.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
    return found


def schema_types(item: dict) -> set[str]:
    raw = item.get("@type", "")
    if isinstance(raw, list):
        return {str(part).lower() for part in raw}
    return {str(raw).lower()} if raw else set()


def schema_name(value: Any) -> str:
    if isinstance(value, dict):
        return clean(value.get("name") or value.get("title"))
    if isinstance(value, list):
        return ", ".join(filter(None, (schema_name(part) for part in value)))
    return clean(value)


def fill_from_schema(book: Book, item: dict) -> None:
    types = schema_types(item)
    if not types.intersection({"book", "product", "offer"}):
        return
    book.title = book.title or schema_name(item.get("name") or item.get("headline"))
    book.author = book.author or schema_name(item.get("author") or item.get("creator"))
    book.publisher = book.publisher or schema_name(item.get("publisher") or item.get("brand"))
    book.description = book.description or clean(item.get("description"))
    book.year = book.year or extract_year(
        item.get("copyrightYear") or item.get("datePublished") or item.get("dateCreated")
    )
    pages = item.get("numberOfPages")
    if pages and not book.pages:
        book.pages = str(pages)
    if item.get("isbn"):
        apply_identifier(book, str(item.get("isbn")))
    if item.get("gtin13") or item.get("gtin") or item.get("sku"):
        apply_identifier(book, str(item.get("gtin13") or item.get("gtin") or item.get("sku")))
    book.cover_type = book.cover_type or map_cover(schema_name(item.get("bookFormat") or item.get("format")))
    offers = item.get("offers")
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict):
        currency = str(offers.get("priceCurrency") or "").upper()
        if currency in {"ILS", "IL", "NIS", ""}:
            book.price_ils = book.price_ils or clean(offers.get("price"))
    if item.get("weight"):
        book.weight_kg = book.weight_kg or parse_weight_kg(schema_name(item.get("weight")))
    height = item.get("height")
    width = item.get("width")
    depth = item.get("depth")
    if height:
        book.height_cm = book.height_cm or re.sub(r"[^\d.]", "", schema_name(height))
    if width:
        book.width_cm = book.width_cm or re.sub(r"[^\d.]", "", schema_name(width))
    if depth:
        book.thickness_cm = book.thickness_cm or re.sub(r"[^\d.]", "", schema_name(depth))


def labeled_value_pairs(soup: BeautifulSoup) -> dict[str, str]:
    pairs: dict[str, str] = {}
    root = soup.select_one(
        "#book-inr-main, .product-info-main, .product-essential, main, article, [itemtype*='schema.org/Book']"
    ) or soup

    def remember(label: str, value: str) -> None:
        label = clean(label).rstrip(":")
        value = clean(value)
        if label and value and len(value) < 500:
            pairs.setdefault(label, value)

    for row in soup.select(".flex"):
        if not isinstance(row, Tag):
            continue
        value_el = row.select_one(".meta-value")
        label_el = row.find(["b", "strong"])
        if label_el and value_el:
            remember(label_el.get_text(" ", strip=True), value_el.get_text(" ", strip=True))

    for prop in root.select("span.property, li, .additional-attributes-wrapper li, .product.attribute, tr"):
        if not isinstance(prop, Tag):
            continue
        if prop.select_one(".flex, .meta-value, .multicolumn-card"):
            continue
        strong = prop.find(["strong", "b", "th", "label"])
        if strong:
            label = strong.get_text(" ", strip=True)
            clone = parse_html(str(prop))
            strong_clone = clone.find(["strong", "b", "th", "label"])
            if strong_clone:
                strong_clone.extract()
            value = clone.get_text(" ", strip=True)
            remember(label, value)
            continue
        text = prop.get_text(" ", strip=True)
        if ":" in text and len(text) < 180:
            label, value = text.split(":", 1)
            remember(label, value)

    for dt in root.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            remember(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
    for title in soup.select(".attributeList-title"):
        content = title.find_next_sibling(class_="attributeList-content")
        if content:
            remember(title.get_text(" ", strip=True), content.get_text(" ", strip=True))
    return pairs


def match_label(label: str) -> str | None:
    compact = _normalize_label(label)
    for field, aliases in LABEL_MAP.items():
        for alias in aliases:
            if _normalize_label(alias) == compact:
                return field
    return None


def _normalize_label(text: str) -> str:
    text = clean(text).lower()
    text = text.replace("״", '"').replace("׳", "'")
    return re.sub(r"[^a-z0-9\u0590-\u05ff]+", "", text)


def fill_from_labels(book: Book, pairs: dict[str, str]) -> None:
    from field_map import apply_pairs

    apply_pairs(book, pairs)


def fill_from_booknet(book: Book, soup: BeautifulSoup, url: str) -> None:
    root = soup.select_one("#book-inr-main, [itemtype*='schema.org/Book']")
    if not root:
        return
    title = root.select_one("#product-page-title, [itemprop='name']")
    if title:
        book.title = book.title or title.get_text(" ", strip=True)
    authors = [a.get_text(" ", strip=True) for a in root.select("[itemprop='author'], .pp-authors a, .product-author")]
    if authors and not book.author:
        book.author = ", ".join(dict.fromkeys(authors))
    publisher = root.select_one("#product-page-manufacturer-name, [itemprop='publisher']")
    if publisher:
        book.publisher = book.publisher or publisher.get_text(" ", strip=True)
    price = root.select_one("#product-page-price")
    if price:
        book.price_ils = book.price_ils or price.get("data-price") or parse_price(price.get_text(" ", strip=True))
    summary = root.select_one("#itemSummary")
    if summary:
        text = summary.get_text("\n", strip=True)
        book.pages = book.pages or parse_pages(text)
        book.description = book.description or text
        book.cover_type = book.cover_type or map_cover(text)
    apply_identifier(book, url)
    for img in root.select("img[data-original], img[src]"):
        src = img.get("data-original") or img.get("src") or ""
        code_match = re.search(r"(\d{10,13})", src)
        if code_match:
            apply_identifier(book, code_match.group(1))
            break


def fill_from_magento(book: Book, soup: BeautifulSoup) -> None:
    title = soup.select_one(".page-title-wrapper h1, .product-info-main h1 .base")
    if title:
        book.title = book.title or title.get_text(" ", strip=True)
    for row in soup.select(".product-attribute-specs-table tr, .additional-attributes-wrapper tr"):
        label = row.find("th")
        value = row.find("td")
        if label and value:
            fill_from_labels(book, {label.get_text(" ", strip=True): value.get_text(" ", strip=True)})
    price = soup.select_one("[data-price-amount], .price-wrapper .price")
    if price and not book.price_ils:
        book.price_ils = price.get("data-price-amount") or parse_price(price.get_text(" ", strip=True))


def extract_book_from_html(html: str, url: str) -> Book:
    soup = parse_html(html)
    book = Book(url=url)
    for item in json_ld_objects(html):
        fill_from_schema(book, item)
    fill_from_booknet(book, soup, url)
    fill_from_magento(book, soup)
    from field_map import attach_page_fields, collect_extra_pairs, remember_candidates

    pairs = labeled_value_pairs(soup)
    pairs.update(collect_extra_pairs(soup, html))
    fill_from_labels(book, pairs)
    attach_page_fields(book, pairs)
    remember_candidates(pairs, url)
    if not book.title:
        og = soup.select_one("meta[property='og:title']")
        h1 = soup.find("h1")
        book.title = (og.get("content") if og else "") or (h1.get_text(" ", strip=True) if h1 else "")
        book.title = re.sub(r"\s+\|.*$", "", book.title).strip()
    if not book.description:
        ogd = soup.select_one("meta[property='og:description']")
        if ogd:
            book.description = clean(ogd.get("content"))
    apply_identifier(book, url)
    book.cover_type = book.cover_type or map_cover(book.title)
    prefer_catalog_price(book, soup)
    book.year = extract_year(book.year)
    book.title = clean(book.title)
    book.author = format_person_name(book.author) or clean(book.author)
    book.publisher = clean(book.publisher)
    book.description = clean(book.description)
    fill_book_images(book, soup, url, html)
    book.mark_origin_fields()
    return book


SEARCH_PATH_HINTS = ("/search", "/catalogsearch", "/חיפוש", "/advancedsearch")
SEARCH_QUERY_KEYS = ("q", "s", "query", "keyword")
SEARCH_HEADING_RE = re.compile(
    r"(תוצאות\s*חיפוש|חיפוש\s*:|search\s*results|\d+\s+results?\s+for)",
    re.I,
)
HEBREW_VOLUME_LETTERS = {
    "א": "1",
    "ב": "2",
    "ג": "3",
    "ד": "4",
    "ה": "5",
    "ו": "6",
    "ז": "7",
    "ח": "8",
    "ט": "9",
    "י": "10",
}


def is_query_listing_url(url: str) -> bool:
    """True for publisher search/query pages such as /search?q=series-name."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    path_l = path.lower()
    qs = parse_qs(parsed.query)
    has_query = any((qs.get(key) or [""])[0] for key in SEARCH_QUERY_KEYS)
    if any(hint in path or hint in path_l for hint in SEARCH_PATH_HINTS):
        return True
    if not has_query:
        return False
    stripped = path.rstrip("/") or "/"
    if any(hint in path_l or hint in path for hint in PRODUCT_PATH_HINTS) and stripped.count("/") >= 2:
        return False
    return True


def is_search_results_page(soup: BeautifulSoup, url: str) -> bool:
    if is_query_listing_url(url):
        return True
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.find("h1")
    blob = f"{title} {heading.get_text(' ', strip=True) if heading else ''}"
    return bool(SEARCH_HEADING_RE.search(blob))


def is_product_page(soup: BeautifulSoup, url: str) -> bool:
    if is_query_listing_url(url):
        return False
    if soup.select_one("#book-inr-main, #product-page-title, [itemtype*='schema.org/Book']"):
        return True
    if soup.select_one(".catalog-product-view, .product-info-main, #product_addtocart_form"):
        return True
    path = urlparse(url).path.lower()
    return any(hint in path for hint in ("/מוצרים/", "/product/", "/products/", "/product-page/", "/book/"))


def series_volume(text: str) -> str:
    name = normalize_name(text)
    if not name:
        return ""
    labeled = re.search(r"(?:חלק|כרך|ספר|volume|vol)\s+(\d{1,2})\b", name)
    if labeled:
        return labeled.group(1)
    labeled_he = re.search(r"(?:חלק|כרך|ספר)\s+([א-י])\b", name)
    if labeled_he:
        return HEBREW_VOLUME_LETTERS.get(labeled_he.group(1), "")
    middle = re.search(r"\s+(\d{1,2})\s+(?:-|–|—)", name)
    if middle:
        return middle.group(1)
    trailing = re.search(r"\b(\d{1,2})$", name)
    if trailing:
        return trailing.group(1)
    trailing_he = re.search(r"\b([א-י])$", name)
    if trailing_he and len(name.split()) >= 2:
        return HEBREW_VOLUME_LETTERS.get(trailing_he.group(1), "")
    numbers = re.findall(r"\b(\d{1,2})\b", name)
    if numbers:
        return numbers[-1]
    return ""


def series_title_core(text: str) -> str:
    name = normalize_name(text)
    name = re.sub(r"\s+(?:חלק|כרך|ספר|volume|vol)\s+(?:\d{1,2}|[א-י])$", "", name)
    name = re.sub(r"\s+\d{1,2}$", "", name)
    return name.strip()


def search_result_rank(wanted: Book, card_title: str, url: str) -> int | None:
    """Lower is better. None means the card is unrelated to the wanted book."""
    for code in (wanted.isbn, wanted.danacode, wanted.upc):
        if code and code in (url or ""):
            return 0
    want = normalize_name(wanted.display_title())
    card = normalize_name(card_title)
    want_core = series_title_core(wanted.display_title())
    card_core = series_title_core(card_title)
    want_vol = series_volume(wanted.display_title())
    card_vol = series_volume(card_title)
    if not card:
        return 80
    if want and card == want:
        return 1
    core_hit = bool(
        want_core
        and card_core
        and len(want_core) >= 6
        and (want_core == card_core or want_core in card_core or card_core in want_core)
    )
    contain_hit = bool(want and card and len(min(want, card, key=len)) >= 6 and (want in card or card in want))
    vol_match = bool(want_vol and card_vol and want_vol == card_vol)
    vol_mismatch = bool(want_vol and card_vol and want_vol != card_vol)
    if core_hit and vol_match:
        return 2
    if contain_hit and vol_match:
        return 3
    if core_hit and not vol_mismatch:
        return 4
    if contain_hit and not vol_mismatch:
        return 5
    if core_hit:
        return 40
    if contain_hit:
        return 50
    return None


def same_domain(left: str, right: str) -> bool:
    return urlparse(left).netloc.lstrip("www.") == urlparse(right).netloc.lstrip("www.")


def normalize_url(base: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    absolute = urljoin(base, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    if any(bit in parsed.path.lower() for bit in SKIP_PATH_BITS):
        return None
    cleaned = parsed._replace(fragment="")
    return urlunparse(cleaned)


def _largest_srcset_url(srcset: str) -> str:
    best = ""
    best_width = -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        width = 0
        if len(bits) > 1 and bits[1].lower().endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                width = 0
        if width >= best_width:
            best_width = width
            best = url
    return best


def _image_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.casefold().rstrip("/")
    return path or url.casefold()


def _absolute_image_url(page_url: str, raw: str) -> str:
    href = (raw or "").strip()
    if not href or href.startswith("data:"):
        return ""
    if href.startswith("//"):
        href = "https:" + href
    absolute = urljoin(page_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return urlunparse(parsed._replace(fragment=""))


def _image_url_from_tag(img: Tag, page_url: str) -> str:
    srcset = str(img.get("srcset") or img.get("data-srcset") or "")
    raw = _largest_srcset_url(srcset) if srcset else ""
    if not raw:
        raw = str(
            img.get("data-zoom-src")
            or img.get("data-original")
            or img.get("data-src")
            or img.get("content")
            or img.get("src")
            or ""
        )
    return _absolute_image_url(page_url, raw.split(" ")[0])


def fill_book_images(book: Book, soup: BeautifulSoup, page_url: str, html: str = "") -> None:
    skip_bits = ("logo", "icon", "sprite", "placeholder", "pixel", "blank", "spacer", "avatar", "badge", "payment")
    cover_hints = ("cover", "front", "כריכה", "קדמ")
    back_hints = ("back", "rear", "גב הספר", "אחור", "גב ")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def remember(url: str, hint: str) -> None:
        url = (url or "").strip()
        if not url:
            return
        key = _image_key(url)
        if not key or key in seen:
            return
        blob = f"{url} {hint}".casefold()
        if any(bit in blob for bit in skip_bits):
            return
        seen.add(key)
        found.append((url, hint.casefold()))

    selectors = (
        ".product__media img, media-gallery img, .product-gallery img, "
        "#book-inr-main img, .product.media img, .fotorama img, "
        "[data-media-id] img, .gallery img, img[itemprop='image'], "
        ".product__media-item img, .product-image img"
    )
    for img in soup.select(selectors):
        if not isinstance(img, Tag):
            continue
        hint_parts = [
            str(img.get("alt") or ""),
            " ".join(img.get("class") or []),
            str(img.get("id") or ""),
        ]
        parent = img.parent
        if isinstance(parent, Tag):
            hint_parts.append(" ".join(parent.get("class") or []))
            hint_parts.append(parent.get_text(" ", strip=True)[:80])
        remember(_image_url_from_tag(img, page_url), " ".join(hint_parts))

    for tag in soup.select(
        ".product__media a[href], media-gallery a[href], .product-gallery a[href], "
        "#book-inr-main a[href], [data-media-id] a[href]"
    ):
        href = str(tag.get("href") or "")
        if re.search(r"\.(?:jpe?g|png|webp|gif)(?:\?|$)", href, re.I):
            remember(_absolute_image_url(page_url, href), str(tag.get("aria-label") or tag.get_text(" ", strip=True)))

    og = soup.select_one("meta[property='og:image'], meta[name='og:image']")
    if og:
        remember(_absolute_image_url(page_url, str(og.get("content") or "")), "cover og")

    for item in json_ld_objects(html or ""):
        image = item.get("image")
        urls = image if isinstance(image, list) else [image] if image else []
        for entry in urls:
            if isinstance(entry, dict):
                remember(_absolute_image_url(page_url, str(entry.get("url") or entry.get("contentUrl") or "")), "schema")
            else:
                remember(_absolute_image_url(page_url, str(entry or "")), "schema")

    cover = ""
    back = ""
    leftovers: list[str] = []
    for url, hint in found:
        if not cover and any(token in hint or token in url.casefold() for token in cover_hints):
            cover = url
            continue
        if not back and any(token in hint or token in url.casefold() for token in ("backcover", "back-cover", "rear", "אחור")):
            back = url
            continue
        if not back and ("גב" in hint and "לוגו" not in hint):
            back = url
            continue
        leftovers.append(url)
    if not cover and leftovers:
        cover = leftovers.pop(0)
    if not back and leftovers:
        back = leftovers[0]
    if cover and not book.cover_image_url:
        book.cover_image_url = cover
    if back and _image_key(back) != _image_key(cover) and not book.back_image_url:
        book.back_image_url = back


def slugs_from_title(title: str) -> list[str]:
    text = clean(title)
    text = re.sub(r"[\"'״׳.,!?()[\]{}]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    bases = [text]
    for sep in (" - ", " – ", " | ", ": ", " / "):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if head and head not in bases:
                bases.append(head)
    words = text.split()
    if len(words) > 5:
        bases.append(" ".join(words[:5]))
    slugs: list[str] = []
    for base in bases:
        slugs.extend(
            [
                base.replace(" ", "_"),
                base.replace(" ", "-"),
                base.replace(" ", "_").replace("-", "_"),
                base.replace(" ", "-").replace("_", "-"),
            ]
        )
    return list(dict.fromkeys(slugs))


def collect_matching_links(soup: BeautifulSoup, page_url: str, book: Book) -> list[str]:
    want = normalize_name(book.display_title())
    if len(want) < 6:
        return []
    found: list[str] = []
    skip = {
        "contact",
        "cart",
        "login",
        "signup",
        "authors",
        "search",
        "home",
        "about",
        "privacy",
        "account",
        "wishlist",
        "collections",
        "pages",
    }
    for tag in soup.select("a[href]"):
        url = normalize_url(page_url, tag.get("href", ""))
        if not url or not same_domain(page_url, url):
            continue
        path = unquote(urlparse(url).path).strip("/")
        first = path.split("/")[0].casefold() if path else ""
        if first in skip:
            continue
        text = normalize_name(tag.get_text(" ", strip=True))
        slug = normalize_name(path.replace("/", " ").replace("-", " ").replace("_", " "))
        if want in text or want in slug or (len(text) >= 8 and text in want):
            if url not in found:
                found.append(url)
    return found


def collect_product_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    found: list[str] = []

    def add(href: str | None) -> None:
        url = normalize_url(page_url, href or "")
        if url and "/icons/" not in url and url not in found:
            path = urlparse(url).path.lower()
            if any(hint in path for hint in PRODUCT_PATH_HINTS):
                found.append(url)

    selectors = [
        ".book-item a[href*='/מוצרים/']",
        ".product-cube a[href*='/מוצרים/']",
        ".product-item-info a.product-item-link",
        ".product-item a.product-item-photo",
        "a[href*='/מוצרים/']",
        "a[href*='/product/']",
        "a[href*='/products/']",
        "a[href*='/product-page/']",
        "a[href*='/catalog/product/']",
        "a[href*='/book/']",
        "a[href*='/ספר/']",
    ]
    for selector in selectors:
        for tag in soup.select(selector):
            add(tag.get("href", ""))
    html = str(soup)
    for match in PRODUCT_HREF_RE.finditer(html):
        add(match.group(1))
    if found:
        return found
    for tag in soup.select("a[href]"):
        href = tag.get("href", "")
        path = urlparse(urljoin(page_url, href)).path
        if any(hint in path for hint in PRODUCT_PATH_HINTS) and "/קטגוריות/" not in path:
            add(href)
    return found


def _url_key(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path).rstrip("/") or "/"
    host_path = f"{parsed.netloc.lstrip('www.')}{path}".casefold()
    if not is_query_listing_url(url):
        return host_path
    qs = parse_qs(parsed.query)
    query = ""
    for key in SEARCH_QUERY_KEYS:
        if qs.get(key):
            query = qs[key][0]
            break
    return f"{host_path}?q={normalize_name(query)}"


def collect_search_result_links(soup: BeautifulSoup, page_url: str, book: Book) -> list[str]:
    """Product links on a ?q= search page, ranked toward the matching series volume."""
    current = _url_key(page_url)
    cards: dict[str, tuple[str, str]] = {}

    def add(href: str | None, title_text: str = "") -> None:
        url = normalize_url(page_url, href or "")
        if not url or not same_domain(page_url, url) or is_query_listing_url(url):
            return
        key = _url_key(url)
        if key == current:
            return
        path = unquote(urlparse(url).path)
        productish = any(hint in path.lower() or hint in path for hint in PRODUCT_PATH_HINTS)
        title_text = clean(title_text)
        if not productish and not title_text:
            return
        previous = cards.get(key)
        if previous is None:
            cards[key] = (url, title_text)
            return
        old_title = previous[1]
        old_vol = bool(series_volume(old_title))
        new_vol = bool(series_volume(title_text))
        if (new_vol and not old_vol) or (new_vol == old_vol and len(title_text) > len(old_title)):
            cards[key] = (url, title_text)

    for selector in (
        "h3.card__heading a[href]",
        "a.full-unstyled-link[href]",
        "a.product-item-link[href]",
        ".product-item-info a[href]",
        ".product-card a[href]",
        ".book-item a[href]",
        ".grid-product__title a[href]",
        "a.product-title[href]",
    ):
        for tag in soup.select(selector):
            add(tag.get("href"), tag.get_text(" ", strip=True))
    for img in soup.find_all("img"):
        parent = img.find_parent("a")
        if parent:
            add(parent.get("href"), img.get("alt") or "")
    for url in collect_product_links(soup, page_url):
        add(url)

    ranked: list[tuple[tuple[int, int, int], str]] = []
    for url, title_text in cards.values():
        rank = search_result_rank(book, title_text, url)
        if rank is None:
            continue
        volume = series_volume(title_text)
        tie = int(volume) if volume.isdigit() else 99
        ranked.append(((rank, tie, -len(title_text)), url))
    ranked.sort()
    strong = [url for (rank, _tie, _length), url in ranked if rank < 40]
    if strong:
        return list(dict.fromkeys(strong))
    return list(dict.fromkeys(url for _score, url in ranked))


def collect_detail_links(soup: BeautifulSoup, page_url: str, book: Book) -> list[str]:
    """Links that typically go from a teaser/card to the real book page."""
    current = _url_key(page_url)
    ranked: list[tuple[tuple[int, int, int], str]] = []
    seen: set[str] = set()
    title = normalize_name(book.display_title())

    def add(href: str | None, image: bool = False) -> None:
        url = normalize_url(page_url, href or "")
        if not url or not same_domain(page_url, url):
            return
        key = _url_key(url)
        if key == current or key in seen:
            return
        path = unquote(urlparse(url).path)
        if path.rstrip("/") in {"", "/"}:
            return
        seen.add(key)
        slug = normalize_name(path.replace("/", " ").replace("-", " ").replace("_", " "))
        productish = any(hint in path.lower() or hint in path for hint in PRODUCT_PATH_HINTS)
        titleish = bool(title) and (title in slug or (len(slug) >= 8 and slug in title))
        depth = len([part for part in path.split("/") if part])
        rank = (
            0 if image and titleish else 1 if image else 2 if titleish else 3 if productish else 4,
            -depth,
            -len(path),
        )
        ranked.append((rank, url))

    canonical = soup.select_one("link[rel='canonical']")
    if canonical:
        add(canonical.get("href"))
    og_url = soup.select_one("meta[property='og:url']")
    if og_url:
        add(og_url.get("content"))
    for img in soup.find_all("img"):
        parent = img.find_parent("a")
        if parent:
            add(parent.get("href"), image=True)
    for tag in soup.select(
        "a.product-item-photo, a.product-image-wrapper, a.product-item-link, "
        ".product-image a, .book-cover a, .book-item a, figure a"
    ):
        add(tag.get("href"), image=True)
    for url in collect_matching_links(soup, page_url, book):
        add(url)
    for url in collect_product_links(soup, page_url):
        add(url)
    ranked.sort(key=lambda item: item[0])
    return [url for _rank, url in ranked]


def fillable_count(book: Book) -> int:
    return sum(1 for name in FILLABLE_FIELDS if str(getattr(book, name, "") or "").strip())


def fills_needed(candidate: Book, wanted: Book) -> int:
    count = 0
    for name in FILLABLE_FIELDS:
        if str(getattr(wanted, name, "") or "").strip():
            continue
        if str(getattr(candidate, name, "") or "").strip():
            count += 1
    return count


def page_number(url: str) -> int:
    qs = parse_qs(urlparse(url).query)
    raw = (qs.get("page") or qs.get("p") or ["1"])[0]
    return int(raw) if str(raw).isdigit() else 1


def sequential_listing_urls(page_url: str, max_pages: int) -> list[str]:
    parsed = urlparse(page_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    current = page_number(page_url)
    urls: list[str] = []
    for number in range(1, max_pages + 1):
        if number == current:
            continue
        items = [
            (key, value)
            for key, values in qs.items()
            if key not in {"page", "p", ""}
            for value in values
            if value
        ]
        items.append(("page", str(number)))
        urls.append(urlunparse(parsed._replace(query=urlencode(items, safe="/"))))
    return urls


def collect_pagination_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    links: list[str] = []
    start = urlparse(page_url)
    for tag in soup.select(".pagination a[href], .pages a[href], a[rel='next'], a.page-next, a.num"):
        url = normalize_url(page_url, tag.get("href", ""))
        if not url:
            continue
        parsed = urlparse(url)
        same_list = unquote(parsed.path).rstrip("/") == unquote(start.path).rstrip("/")
        if not same_list and "page" not in parse_qs(parsed.query):
            continue
        if url not in links:
            links.append(url)
    if links:
        return sequential_listing_urls(page_url, 40)
    return links


class BookCrawler:
    def __init__(
        self,
        delay_seconds: float = 0.35,
        timeout: int = 25,
        cancelled: Callable[[], bool] | None = None,
        progress: ProgressFn | None = None,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.cancelled = cancelled or (lambda: False)
        self.progress = progress or (lambda _msg: None)
        self.report = CrawlReport()
        self.session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504))
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "he,en;q=0.8",
            }
        )

    def _check_cancel(self) -> None:
        if self.cancelled():
            raise CrawlCancelled("Search cancelled")

    def fetch(self, url: str) -> tuple[str, str]:
        self._check_cancel()
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        time.sleep(self.delay_seconds)
        return response.text, response.url

    def _book_and_html(self, product_url: str, remember: bool = True) -> tuple[Book | None, str, str]:
        from book_cache import get_page_book, remember_page_book, save_page_cache

        cached = get_page_book(product_url)
        if cached and cached.title:
            self.report.product_cached += 1
            return cached, "", cached.url
        html, resolved = self.fetch(product_url)
        book = extract_book_from_html(html, resolved)
        self.report.product_fetched += 1
        if remember and book.title and not is_query_listing_url(resolved):
            remember_page_book(book)
            save_page_cache()
        return book, html, resolved

    def _book_from_url(self, product_url: str, remember: bool = True) -> Book | None:
        book, _html, _resolved = self._book_and_html(product_url, remember=remember)
        return book

    def crawl(
        self,
        start_url: str,
        year: str,
        max_listing_pages: int = 5,
        max_products: int = 150,
        include_unknown_year: bool = True,
    ) -> list[Book]:
        start_url = start_url.strip()
        if not start_url.startswith(("http://", "https://")):
            start_url = "https://" + start_url
        results: list[Book] = []
        seen_products: set[str] = set()
        try:
            html, final_url = self.fetch(start_url)
            self.report.listing_pages += 1
        except requests.RequestException:
            self.report.listing_failed += 1
            self.progress("Could not open the start URL.")
            from field_map import flush_candidates

            flush_candidates()
            return results
        soup = parse_html(html)

        if is_product_page(soup, final_url):
            book = extract_book_from_html(html, final_url)
            self.report.product_fetched += 1
            self.report.product_links = 1
            if book.title:
                from book_cache import remember_page_book, save_page_cache

                remember_page_book(book)
                save_page_cache()
                book.append_scan_log("Opened a product page and read the book.")
                book.refresh_scan_status()
                if book.matches_year(year, include_unknown_year):
                    results.append(book)
                    self.report.matched += 1
                else:
                    self.report.skipped_year += 1
                    book.append_scan_log(f"Skipped: publication year {book.year or 'unknown'} is not {year}.")
            else:
                book.mark_scan_failed("Opened the product page but could not read a title.")
                results.append(book)
                self.report.product_failed += 1
            self.progress(f"Opened a product page. Found {len(results)} matching book(s).")
            from field_map import flush_candidates

            flush_candidates()
            return results

        listing_pages = [final_url]
        product_urls: list[str] = []
        visited_listings: set[str] = set()

        try:
            for listing_url in listing_pages:
                self._check_cancel()
                if listing_url in visited_listings or len(visited_listings) >= max_listing_pages:
                    continue
                visited_listings.add(listing_url)
                self.progress(
                    f"Reading listing page {len(visited_listings)}/{max_listing_pages}…"
                )
                if listing_url != final_url:
                    try:
                        html, listing_url = self.fetch(listing_url)
                        self.report.listing_pages += 1
                        soup = parse_html(html)
                    except requests.RequestException:
                        self.report.listing_failed += 1
                        continue
                for product_url in collect_product_links(soup, listing_url):
                    if same_domain(start_url, product_url) and product_url not in product_urls:
                        product_urls.append(product_url)
                if len(visited_listings) < max_listing_pages:
                    for page_url in collect_pagination_links(soup, listing_url):
                        if same_domain(start_url, page_url) and page_url not in listing_pages:
                            listing_pages.append(page_url)
                if len(product_urls) >= max_products:
                    break

            product_urls = product_urls[:max_products]
            self.report.product_links = len(product_urls)
            self.progress(f"Found {len(product_urls)} book pages. Checking publication year {year}…")

            for index, product_url in enumerate(product_urls, start=1):
                self._check_cancel()
                if product_url in seen_products:
                    continue
                seen_products.add(product_url)
                try:
                    book = self._book_from_url(product_url)
                except requests.RequestException as exc:
                    self.report.product_failed += 1
                    stub = Book(url=product_url)
                    stub.mark_scan_failed(f"Could not open the book page: {exc.__class__.__name__}: {exc}")
                    results.append(stub)
                    self.progress(f"Kept a failed page ({exc.__class__.__name__}). Continuing…")
                    continue
                if book is None:
                    stub = Book(url=product_url)
                    stub.mark_scan_failed("The book page returned no data.")
                    results.append(stub)
                    self.report.product_failed += 1
                elif not book.title:
                    book.mark_scan_failed("Opened the page but could not read a title.")
                    results.append(book)
                    self.report.product_failed += 1
                elif book.matches_year(year, include_unknown_year):
                    book.append_scan_log("Read the book page.")
                    book.refresh_scan_status()
                    results.append(book)
                    self.report.matched += 1
                else:
                    self.report.skipped_year += 1
                self.progress(
                    f"Checked {index}/{len(product_urls)} books — {len(results)} listed for {year or 'any year'}"
                )
        except CrawlCancelled:
            self.report.cancelled = True
            self.progress(f"Stopped. Keeping {len(results)} matching book(s) found so far.")
        from field_map import flush_candidates

        flush_candidates()
        return results

    def search_urls_for_query(self, site_url: str, query: str) -> list[str]:
        parsed = urlparse(site_url if "://" in site_url else "https://" + site_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        host = parsed.netloc.lower()
        encoded = quote(query)
        if "e-vrit" in host or "evrit" in host:
            return [f"{origin}/search?q={encoded}"]
        if "booknet" in host:
            return [
                f"{origin}/search?q={encoded}",
                f"{origin}/%D7%97%D7%99%D7%A4%D7%95%D7%A9?q={encoded}",
            ]
        return [
            f"{origin}/search?q={encoded}",
            f"{origin}/search?type=product&q={encoded}",
            f"{origin}/catalogsearch/result/?q={encoded}",
            f"{origin}/AdvancedSearch?q={encoded}",
            f"{origin}/?s={encoded}",
        ]

    def _slug_urls_for_book(self, site_url: str, book: Book) -> list[str]:
        parsed = urlparse(site_url if "://" in site_url else "https://" + site_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        urls: list[str] = []
        for slug in slugs_from_title(book.display_title()):
            urls.append(f"{origin}/{quote(slug)}")
        return urls

    def _better_book_page(self, current: Book | None, incoming: Book | None, wanted: Book) -> Book | None:
        if incoming is None:
            return current
        if current is None:
            return incoming
        current_need = fills_needed(current, wanted)
        incoming_need = fills_needed(incoming, wanted)
        if incoming_need != current_need:
            return incoming if incoming_need > current_need else current
        current_all = fillable_count(current)
        incoming_all = fillable_count(incoming)
        if incoming_all != current_all:
            return incoming if incoming_all > current_all else current
        return current

    def _follow_book_cover_links(
        self,
        soup: BeautifulSoup,
        page_url: str,
        wanted: Book,
        seen: set[str],
        depth: int,
        current: Book | None,
    ) -> Book | None:
        best = None if current and is_query_listing_url(current.url) else current
        if depth >= 2:
            return best
        listing = is_search_results_page(soup, page_url)
        if listing:
            product_urls = collect_search_result_links(soup, page_url, wanted)
            if not product_urls:
                product_urls = collect_product_links(soup, page_url)
            limit = 16
        else:
            product_urls = collect_detail_links(soup, page_url, wanted)
            limit = 8
        for product_url in product_urls[:limit]:
            found = self._consider_book_page(product_url, wanted, seen, remember=False, depth=depth + 1)
            if found is None:
                continue
            richer = self._better_book_page(best, found, wanted)
            if richer is not best:
                self.progress(f"Opened the full book page: {richer.url}")
                best = richer
            needed = len(wanted.missing_fields())
            if needed and fills_needed(best, wanted) >= needed:
                break
        return best

    def _consider_book_page(
        self,
        product_url: str,
        wanted: Book,
        seen: set[str],
        remember: bool,
        depth: int = 0,
    ) -> Book | None:
        from book_cache import remember_page_book, save_page_cache

        key = _url_key(product_url)
        if key in seen:
            return None
        seen.add(key)
        try:
            candidate, html, resolved = self._book_and_html(product_url, remember=remember)
        except requests.RequestException:
            return None
        listing = is_query_listing_url(resolved or product_url)
        matched = candidate if candidate and candidate.title and books_match(wanted, candidate) else None
        if matched is not None and listing:
            matched = None
        if matched is None and depth > 0 and not listing:
            return None
        if not html:
            try:
                html, resolved = self.fetch(resolved or product_url)
            except requests.RequestException:
                html = ""
        if html:
            soup = parse_html(html)
            listing = listing or is_search_results_page(soup, resolved)
            if listing:
                if depth == 0:
                    self.progress("Search listed several books — opening a matching volume…")
                matched = self._follow_book_cover_links(soup, resolved, wanted, seen, depth, None)
            elif matched is None:
                matched = self._follow_book_cover_links(soup, resolved, wanted, seen, depth, None)
            elif fills_needed(matched, wanted) < len(wanted.missing_fields()) or not is_product_page(soup, resolved):
                matched = self._follow_book_cover_links(soup, resolved, wanted, seen, depth, matched)
        if matched and is_query_listing_url(matched.url):
            return None
        if matched:
            remember_page_book(matched)
            save_page_cache()
        return matched

    def find_matching_product(self, site_url: str, book: Book, try_slugs: bool = False) -> Book | None:
        from book_cache import cached_books_for_host, remember_page_book, save_page_cache

        parsed = urlparse(site_url if "://" in site_url else "https://" + site_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        host = parsed.netloc
        seen: set[str] = set()
        for candidate in cached_books_for_host(host):
            if not (candidate.title and books_match(book, candidate)):
                continue
            if is_query_listing_url(candidate.url):
                continue
            self.progress(f"Matched from cached {host} pages.")
            if fills_needed(candidate, book) >= len(book.missing_fields()) and fillable_count(candidate) >= 5:
                return candidate
            try:
                html, final_url = self.fetch(candidate.url)
            except requests.RequestException:
                return candidate
            soup = parse_html(html)
            seen.add(_url_key(candidate.url))
            richer = self._follow_book_cover_links(soup, final_url, book, seen, 0, candidate)
            if richer and not is_query_listing_url(richer.url):
                remember_page_book(richer)
                save_page_cache()
                return richer
            return candidate

        def consider(product_url: str, remember: bool) -> Book | None:
            return self._consider_book_page(product_url, book, seen, remember=remember)

        if try_slugs:
            for product_url in self._slug_urls_for_book(site_url, book):
                found = consider(product_url, remember=False)
                if found:
                    self.progress(f"Found book page: {found.url}")
                    return found
            try:
                html, final_url = self.fetch(origin + "/")
                soup = parse_html(html)
                homepage_links = collect_detail_links(soup, final_url, book) or collect_matching_links(
                    soup, final_url, book
                )
                for product_url in homepage_links[:8]:
                    found = consider(product_url, remember=False)
                    if found:
                        self.progress(f"Found book page: {found.url}")
                        return found
            except requests.RequestException:
                pass
        queries = [value for value in (book.isbn, book.display_title(), f"{book.title} {book.author}") if value]
        for query in queries:
            for search_url in self.search_urls_for_query(site_url, query):
                key = _url_key(search_url)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    html, final_url = self.fetch(search_url)
                except requests.RequestException:
                    continue
                soup = parse_html(html)
                if is_search_results_page(soup, final_url):
                    self.progress("Search listed several books — opening a matching volume…")
                    product_urls = collect_search_result_links(soup, final_url, book)
                    if not product_urls:
                        product_urls = collect_product_links(soup, final_url)
                    for product_url in product_urls[:16]:
                        found = consider(product_url, remember=False)
                        if found:
                            self.progress(f"Found book page: {found.url}")
                            return found
                    continue
                page_book = extract_book_from_html(html, final_url)
                matched = page_book if page_book.title and books_match(book, page_book) else None
                if matched:
                    remember_page_book(matched)
                    save_page_cache()
                    richer = self._follow_book_cover_links(soup, final_url, book, seen, 0, matched)
                    if richer:
                        self.progress(f"Found book page: {richer.url}")
                        return richer
                    if is_query_listing_url(matched.url):
                        continue
                    self.progress(f"Found book page: {matched.url}")
                    return matched
                product_urls = collect_detail_links(soup, final_url, book)
                if not product_urls:
                    product_urls = collect_product_links(soup, final_url)
                    for extra in collect_matching_links(soup, final_url, book):
                        if extra not in product_urls:
                            product_urls.append(extra)
                for product_url in product_urls[:8]:
                    found = consider(product_url, remember=False)
                    if found:
                        self.progress(f"Found book page: {found.url}")
                        return found
        return None

    def enrich_one_book(self, book: Book) -> list[str]:
        from publisher_sites import resolve_publisher_site

        filled: list[str] = []
        previous = self.progress

        def progress(msg: str) -> None:
            book.append_scan_log(msg)
            previous(msg)

        self.progress = progress
        try:
            publisher_url = resolve_publisher_site(book.publisher)
            if not publisher_url:
                note = (
                    f"No publisher website is known for {book.publisher}."
                    if book.publisher
                    else "This book has no publisher, so there is no publisher site to search."
                )
                self.progress(note)
                book.mark_publisher_lookup("", note=note)
                book.refresh_scan_status()
                return filled
            host = urlparse(publisher_url).netloc
            book.mark_publisher_lookup(publisher_url, note=f"Looking on {host}…")
            self.progress(f"Looking on the publisher site {host} for “{book.display_title()}”…")
            try:
                match = self.find_matching_product(publisher_url, book, try_slugs=True)
            except CrawlCancelled:
                raise
            except requests.RequestException as exc:
                note = f"Could not open {host}: {exc.__class__.__name__}"
                self.progress(note)
                book.mark_publisher_lookup(publisher_url, note=note)
                book.refresh_scan_status()
                return filled
            if not match:
                note = f"No matching book page found on {host}."
                self.progress(note)
                book.mark_publisher_lookup(publisher_url, note=note)
                book.refresh_scan_status()
                return filled
            added = book.merge_missing(match)
            findings = match.publisher_found_fields() or match._load_field_rows("found_fields")
            if findings:
                book.extra["publisher_found"] = json.dumps(findings, ensure_ascii=False)
                self.progress("Fields found on the publisher book page:")
                for item in findings:
                    mapped = item.get("field") or "not mapped"
                    self.progress(f"  {item['label']}: {item['value']}  [{mapped}]")
            if added:
                note = f"New from {host}: {', '.join(added)}"
                self.progress(note)
                self.report.enriched += 1
            else:
                note = f"Found the book on {host}, but every fillable field was already set."
                self.progress(note)
            if match.url:
                self.progress(f"Publisher book page: {match.url}")
            book.mark_publisher_lookup(publisher_url, page=match.url, filled=added, note=note)
            book.refresh_scan_status()
            return added
        finally:
            self.progress = previous

    def enrich_books(self, books: list[Book], extra_urls: list[str], max_searches: int = 24) -> int:
        from book_cache import cached_books_for_host

        filled = 0
        for extra_url in extra_urls:
            host = urlparse(extra_url).netloc
            extras = cached_books_for_host(host)
            if extras:
                self.progress(f"Filling missing fields from cached {host} pages…")
            else:
                self.progress(f"Filling missing fields from {host}…")
                saved = self.report
                self.report = CrawlReport()
                extras = self.crawl(
                    start_url=extra_url,
                    year="",
                    max_listing_pages=4,
                    include_unknown_year=True,
                )
                extra_report = self.report
                self.report = saved
                self.report.listing_pages += extra_report.listing_pages
                self.report.listing_failed += extra_report.listing_failed
                self.report.product_cached += extra_report.product_cached
                self.report.product_fetched += extra_report.product_fetched
                self.report.product_failed += extra_report.product_failed
            added = merge_catalog(books, extras)
            filled += added
            self.report.enriched += added
            pending = [book for book in books if not book.has_page_on(extra_url)]
            pending.sort(key=lambda book: (0 if book.missing_fields() else 1, book.display_title()))
            searches = 0
            for book in pending:
                if searches >= max_searches:
                    break
                searches += 1
                try:
                    match = self.find_matching_product(extra_url, book)
                except CrawlCancelled:
                    raise
                except requests.RequestException:
                    continue
                if not match:
                    continue
                added_fields = book.merge_missing(match)
                if added_fields:
                    filled += 1
                    self.report.enriched += 1
                    self.progress(f"Filled extra details from {host} for {book.display_title()}")
                else:
                    self.progress(f"Found {host} book page for {book.display_title()}")
        from field_map import flush_candidates

        flush_candidates()
        for book in books:
            if book.title:
                book.refresh_scan_status()
        return filled


def parse_site_urls(text: str) -> list[str]:
    urls: list[str] = []
    for raw in (text or "").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        if value not in urls:
            urls.append(value)
    return urls


def books_as_dicts(books: Iterable[Book]) -> list[dict]:
    return [asdict(book) for book in books]
