"""Crawl a bookstore website and extract catalog fields for a given year."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, FeatureNotFound, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ProgressFn = Callable[[str], None]
EventFn = Callable[[str, dict[str, Any]], None]


def _choose_html_parser() -> str:
    for name in ("lxml", "html.parser"):
        try:
            BeautifulSoup("<p></p>", name)
            return name
        except Exception:
            continue
    return "html.parser"


HTML_PARSER = _choose_html_parser()


def entry_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def format_entry_stamp(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        date, rest = text.split("T", 1)
        return f"{date} {rest.replace('Z', '')[:5]}"
    return text[:16]


def entry_day(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        return text.split("T", 1)[0]
    return text[:10]


def _safe_flush_scan_files() -> None:
    try:
        from book_cache import flush_page_cache

        flush_page_cache()
    except Exception:
        pass
    try:
        from field_map import flush_candidates

        flush_candidates()
    except Exception:
        pass


def parse_html(markup: str) -> BeautifulSoup:
    markup = markup or ""
    try:
        return BeautifulSoup(markup, HTML_PARSER)
    except FeatureNotFound:
        return BeautifulSoup(markup, "html.parser")


class SiteError(Exception):
    """The website returned a failure page or could not be reached."""

    def __init__(self, message: str, url: str = "") -> None:
        super().__init__(message)
        self.url = url


def site_error_message(html: str, status: int | None = None, url: str = "") -> str | None:
    """Return a user-facing error if the response is a dead or WordPress error page."""
    del url
    status = int(status or 0)
    soup = parse_html(html or "")
    title = clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    error_page = soup.select_one("#error-page")
    heading = ""
    source = error_page or soup
    h1 = source.find("h1") if source else None
    if h1:
        heading = clean(h1.get_text(" ", strip=True))
    if not heading and error_page:
        paragraph = error_page.find("p")
        heading = clean(paragraph.get_text(" ", strip=True) if paragraph else "")
    blob = f"{title}\n{heading}\n{(html or '')[:8000]}"
    lower = blob.casefold()
    wp_title = ("wordpress" in title.casefold() and "error" in title.casefold()) or (
        "וורדפרס" in title and "שגיאה" in title
    )
    wp_die = error_page is not None or soup.select_one(".wp-die-message") is not None
    critical = (
        "critical error on this website" in lower
        or "this site is experiencing technical difficulties" in lower
        or "שגיאה קריטית" in blob
    )
    if not (status >= 500 or wp_title or wp_die or critical):
        return None
    detail = heading or title
    if wp_title or wp_die or critical or "וורדפרס" in blob or "wordpress" in title.casefold():
        status_bit = f" (HTTP {status})" if status else ""
        if detail:
            return f"The site reported a WordPress error{status_bit}: {detail}"
        return f"The site reported a WordPress error{status_bit}."
    if status >= 500:
        if detail:
            return f"The site returned HTTP {status}: {detail}"
        return f"The site returned HTTP {status}."
    return None


def http_status_message(status: int, url: str = "") -> str:
    host = urlparse(url).netloc or url
    where = f" ({host})" if host else ""
    if status == 404:
        return f"HTTP 404: the site was reached{where}, but this page was not found."
    if status == 403:
        return f"HTTP 403: the site refused access to this page{where}."
    if status == 401:
        return f"HTTP 401: this page requires a login{where}."
    return f"HTTP {status}: the site returned an error for this page{where}."


def request_failure_message(exc: BaseException, url: str) -> str:
    host = urlparse(url).netloc or url
    if isinstance(exc, requests.Timeout):
        return f"Timed out while opening {host}."
    if isinstance(exc, requests.ConnectionError):
        return f"Could not connect to {host}."
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    if status:
        return http_status_message(int(status), url)
    return f"Could not open {host}: {exc.__class__.__name__}."


def decode_http_text(response: requests.Response) -> str:
    """Decode catalog pages as UTF-8 when possible so Hebrew titles stay readable."""
    raw = response.content or b""
    if not raw:
        return ""
    ctype = (response.headers.get("Content-Type") or "").lower()
    path = urlparse(response.url or "").path.lower()
    charset = ""
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", ctype)
    if match:
        charset = match.group(1).lower().replace("utf8", "utf-8")
    json_body = "json" in ctype or "/api/" in path
    if json_body or charset in {"", "utf-8"}:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    for encoding in (charset, "utf-8", "cp1255", "iso-8859-8", response.apparent_encoding or ""):
        name = (encoding or "").replace("utf8", "utf-8")
        if not name:
            continue
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


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
ASSET_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".webm",
    ".pdf",
)
PRODUCT_PATH_HINTS = (
    "/מוצרים/",
    "/product/",
    "/products/",
    "/catalog/product/",
    "/page_",
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
    "translator",
    "illustrator",
    "marc",
    "ddc",
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
    "translator": (
        "translator",
        "translators",
        "translated by",
        "translation by",
        "מתרגם",
        "מתרגמת",
        "מתרגמים",
        "תרגם",
        "תרגמה",
        "תרגום של",
        "שם המתרגם",
    ),
    "illustrator": (
        "illustrator",
        "illustrators",
        "illustrated by",
        "illustrations by",
        "מאייר",
        "מאיירת",
        "מאיירים",
        "אייר",
        "איירה",
        "צייר",
        "ציירת",
        "איורים",
        "שם המאייר",
    ),
    "marc": (
        "marc",
        "marc21",
        "marc 21",
        "marc code",
        "system number",
        "mms id",
        "mmsid",
        "nli id",
        "מספר מערכת",
        "קוד marc",
        "מספר marc",
        "מספר רשומה",
    ),
    "ddc": (
        "ddc",
        "dewey",
        "dewey decimal",
        "dewey classification",
        "dewey class",
        "dewey class number",
        "classification dewey",
        "דיואי",
        "סיווג דיואי",
        "מספר דיואי",
        "קוד דיואי",
        "דיואי עשרוני",
    ),
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
    "nli.org.il": "National Library",
}

CATALOG_MIN_LISTING_PAGES = 40
UNLIMITED_LISTING_PAGES = 10_000
PAGE_IN_URL_RE = re.compile(r"(?:[?&](?:page|p|pg|bscrp)=|/page(?:/|-))(\d+)", re.I)
LISTING_PAGE_QUERY_KEYS = ("bscrp", "page", "p", "pg", "pagenumber", "pageNumber")
EVRIT_GROUP_RE = re.compile(r"/group/(\d+)(?:/|$)", re.I)
EVRIT_PRODUCT_RE = re.compile(r"/product/(\d+)(?:/|$)", re.I)
SHOPIFY_COLLECTION_RE = re.compile(r"/collections/([^/?#]+)", re.I)
EVRIT_NEW_BOOKS_SLUG = "ספרים-חדשים"
CATALOG_HOME_PATHS = {
    "booknet.co.il": "/ספרים-חדשים",
    "ybook.co.il": "/collections/newest-products",
    "modan.co.il": "/חדש-על-המדף",
    "keter-books.co.il": "/ספרים-חדשים",
    "am-oved.co.il": "/חדשים",
    "kinbooks.co.il": "/ספרים-חדשים",
    "nli.org.il": "/he/search?materialType=books",
}
HOST_PUBLISHERS = {
    "ybook.co.il": "ידיעות ספרים",
    "modan.co.il": "מודן",
    "keter-books.co.il": "כתר",
    "am-oved.co.il": "עם עובד",
    "kinbooks.co.il": "כנרת זמורה דביר",
    "kibutz-poalim.co.il": "הקיבוץ המאוחד",
    "schocken.co.il": "שוקן",
    "matarbooks.co.il": "מטר",
    "ahuzatbayit.co.il": "אחוזת בית",
    "resling.co.il": "רסלינג",
    "babel.co.il": "בבל",
    "magnespress.co.il": "מאגנס",
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


def is_evrit_host(url: str) -> bool:
    host = site_host(url)
    return "e-vrit" in host or host == "evrit.co.il" or host.endswith(".evrit.co.il")


def is_booknet_host(url: str) -> bool:
    return site_host(url) == "booknet.co.il"


def is_nli_host(url: str) -> bool:
    host = site_host(url)
    return host == "nli.org.il" or host.endswith(".nli.org.il")


def is_ybook_host(url: str) -> bool:
    return site_host(url) == "ybook.co.il"


def is_shopify_url(url: str) -> bool:
    path = urlparse(url or "").path.lower()
    return "/collections/" in path or "/products/" in path


def shopify_collection_handle(url: str) -> str:
    match = SHOPIFY_COLLECTION_RE.search(unquote(urlparse(url or "").path))
    return unquote(match.group(1)) if match else ""


def default_publisher_for_host(url: str) -> str:
    return HOST_PUBLISHERS.get(site_host(url), "")


def catalog_listing_url(url: str) -> str:
    """Turn a site homepage into the year catalog listing that Search should read."""
    raw = (url or "").strip()
    if not raw:
        return raw
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    path = unquote(parsed.path).rstrip("/") or "/"
    if is_evrit_host(raw) and path in {"", "/"}:
        return urlunparse(
            parsed._replace(path=f"/group/3/{quote(EVRIT_NEW_BOOKS_SLUG)}", query="", fragment="")
        )
    home = CATALOG_HOME_PATHS.get(site_host(raw), "")
    if home and path in {"", "/"}:
        if "?" in home:
            home_path, query = home.split("?", 1)
        else:
            home_path, query = home, ""
        return urlunparse(parsed._replace(path=home_path, query=query, fragment=""))
    return raw


def listing_url_key(url: str) -> str:
    parsed = urlparse(catalog_listing_url(url))
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path).rstrip("/") or "/"
    return f"{host}{path}".casefold()


def unique_catalog_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in urls:
        value = (raw or "").strip()
        if not value:
            continue
        listing = catalog_listing_url(value)
        key = listing_url_key(listing)
        if key in seen:
            continue
        seen.add(key)
        out.append(listing)
    return out


def evrit_group_id(url: str) -> str:
    if not is_evrit_host(url):
        return ""
    match = EVRIT_GROUP_RE.search(unquote(urlparse(url).path))
    return match.group(1) if match else ""


def evrit_product_id(url: str) -> str:
    if not is_evrit_host(url):
        return ""
    match = EVRIT_PRODUCT_RE.search(unquote(urlparse(url).path))
    return match.group(1) if match else ""


def _plain_markup_text(raw: Any) -> str:
    text = str(raw or "")
    if not text.strip():
        return ""
    if "<" in text:
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</p>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return clean(text)


def _evrit_people(value: Any) -> str:
    if isinstance(value, dict):
        return _plain_markup_text(value.get("Name") or value.get("name") or "")
    if isinstance(value, list):
        names = [_evrit_people(item) for item in value]
        return ", ".join(part for part in names if part)
    return _plain_markup_text(value)


def _evrit_pricing_amount(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    for key in ("priceFinal", "unitPrice", "priceBefore", "Price", "price"):
        raw = block.get(key)
        if raw in (None, ""):
            continue
        amount = format_price(str(raw))
        if amount and float(amount) > 0:
            return amount
    return ""


def _evrit_pricing_block(item: dict[str, Any]) -> dict[str, Any]:
    for key in ("ProductPricing", "ProductFormatPricing"):
        block = item.get(key)
        if isinstance(block, dict):
            return block
    return item


def evrit_preferred_price(pricing: Any) -> tuple[str, str]:
    """Return (price, kind). The printed / cover price is the catalog price, not digital."""
    if not isinstance(pricing, dict):
        return "", ""
    block = _evrit_pricing_block(pricing) if "PrintedPricing" not in pricing and "DigitalPricing" not in pricing else pricing
    if "PrintedPricing" not in block and "DigitalPricing" not in block:
        block = _evrit_pricing_block(block)
    printed = _evrit_pricing_amount(block.get("PrintedPricing"))
    if printed:
        return printed, "print"
    retail = block.get("RetailPrice")
    if retail not in (None, ""):
        amount = format_price(str(retail))
        if amount and float(amount) > 0:
            return amount, "print"
    digital = _evrit_pricing_amount(block.get("DigitalPricing"))
    if digital:
        return digital, "digital"
    return "", ""


def evrit_image_url(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    return "https://images.e-vrit.co.il/" + value.lstrip("/")


def _evrit_page_count(*values: Any) -> str:
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        digits = text if text.isdigit() else (parse_pages(text) or re.sub(r"\D", "", text))
        if digits and digits.isdigit() and int(digits) > 0:
            return digits
    return ""


def _evrit_catalog_year(item: dict[str, Any]) -> str:
    """Publication year from e-vrit catalog fields, not from the book description."""
    for key in ("PublishYear", "PublishDate", "DatePublished", "PublishedDate"):
        year = extract_year(str(item.get(key) or ""))
        if year:
            return year
    month = str(item.get("PublishMonth") or "").strip()
    year = extract_year(str(item.get("PublishYear") or ""))
    if year:
        return year
    if month:
        return extract_year(month)
    return ""


def fill_from_evrit_payload(book: Book, item: dict[str, Any]) -> None:
    """Map e-vrit catalog JSON (listing, product, or extra-details) onto a book.

    On a product such as https://www.e-vrit.co.il/product/39436 the site splits data:
    listing/product JSON has printed vs digital prices; extra/1 has PublishDate
    (e.g. מרץ 2026); extra/2 has NumOfPrintedPages. Schema.org offers the digital price.
    """
    if not isinstance(item, dict):
        return
    for key in ("ProductName", "Name", "Title", "BookName", "DisplayName"):
        name = _plain_markup_text(item.get(key))
        if name:
            book.title = book.title or name
            break
    authors = _evrit_people(item.get("Authors") or item.get("Author") or item.get("AuthorName"))
    if authors:
        book.author = book.author or authors
    publishers = _evrit_people(item.get("Publishers") or item.get("Publisher"))
    if publishers:
        book.publisher = book.publisher or publishers
    year = _evrit_catalog_year(item)
    if year:
        book.year = year
        publish_date = str(item.get("PublishDate") or "").strip()
        if publish_date:
            book.set_captured("year", publish_date)
    printed_pages = _evrit_page_count(item.get("NumOfPrintedPages"))
    listing_pages = _evrit_page_count(item.get("NumOfPages"), item.get("Pages"))
    pages = printed_pages or listing_pages
    if pages:
        book.pages = pages
    price, kind = evrit_preferred_price(_evrit_pricing_block(item))
    if price and (kind == "print" or not book.price_ils):
        book.price_ils = price
    if not book.description:
        book.description = _plain_markup_text(item.get("ShortDescription") or item.get("LongDescription") or "")
    image = evrit_image_url(item.get("ProductImage") or item.get("Image"))
    if image and not book.cover_image_url:
        book.cover_image_url = image
    for key in ("Isbn", "ISBN", "ISBN13", "Barcode", "DanaCode", "Danacode"):
        apply_identifier(book, str(item.get(key) or ""))


def fill_from_evrit_html(book: Book, soup: BeautifulSoup, url: str) -> None:
    """e-vrit HTML shows digital first; the printed edition is the catalog price we want."""
    if not is_evrit_host(url):
        return
    printed_price = ""
    for tag in soup.select("[aria-label]"):
        label = str(tag.get("aria-label") or "")
        if "מודפס" not in label:
            continue
        printed_price = parse_price(label)
        if printed_price:
            break
    if not printed_price:
        icon = soup.select_one('[data-icon-type="print"]')
        if icon:
            box = icon.find_parent(attrs={"role": "button"}) or icon.parent
            if box:
                printed_price = parse_price(box.get_text(" ", strip=True))
    if not printed_price:
        for node in soup.find_all(string=re.compile(r"גב\s*הספר")):
            parent = getattr(node, "parent", None)
            if parent is None:
                continue
            printed_price = parse_price(parent.get_text(" ", strip=True))
            if printed_price:
                break
    if printed_price:
        book.price_ils = printed_price


@dataclass
class Book:
    url: str
    title: str = ""
    title_en: str = ""
    title_phonetic: str = ""
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
    translator: str = ""
    illustrator: str = ""
    marc: str = ""
    ddc: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    scanner_id: str = ""
    scan_status: str = ""
    scan_message: str = ""
    approved: bool = False
    excel_passed: bool = False
    final: bool = False
    created_at: str = ""
    modified_at: str = ""
    database_passed_at: str = ""

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

    def stamp_created(self, when: str = "") -> None:
        stamp = (when or "").strip() or entry_now()
        if not (self.created_at or "").strip():
            self.created_at = stamp
        if not (self.modified_at or "").strip():
            self.modified_at = self.created_at

    def fill_missing_dates(self, when: str = "") -> bool:
        """Give created and updated the same timestamp when either is missing."""
        stamp = (when or "").strip() or entry_now()
        created = (self.created_at or "").strip()
        modified = (self.modified_at or "").strip()
        changed = False
        if not created and not modified:
            self.created_at = stamp
            self.modified_at = stamp
            return True
        if not created:
            self.created_at = modified or stamp
            changed = True
        if not (self.modified_at or "").strip():
            self.modified_at = self.created_at or stamp
            changed = True
        return changed

    def stamp_modified(self) -> None:
        stamp = entry_now()
        if not (self.created_at or "").strip():
            self.created_at = stamp
        self.modified_at = stamp

    def stamp_database_passed(self) -> None:
        self.database_passed_at = entry_now()
        self.excel_passed = True

    def database_needs_update(self) -> bool:
        passed = (self.database_passed_at or "").strip()
        modified = (self.modified_at or "").strip()
        return bool(passed and modified and modified > passed)

    def was_transferred(self) -> bool:
        return bool((self.database_passed_at or "").strip() or self.excel_passed)

    def updated_after_created(self) -> bool:
        created = (self.created_at or "").strip()
        modified = (self.modified_at or "").strip()
        if not created or not modified:
            return False
        created_day = entry_day(created)
        modified_day = entry_day(modified)
        if created_day and modified_day and modified_day > created_day:
            return True
        return modified > created

    def is_important(self) -> bool:
        """Needs the owner's attention for a first pass or another database pass."""
        if not (self.title or "").strip():
            return False
        if not self.was_transferred():
            return True
        if self.database_needs_update():
            return True
        if self.updated_after_created() and not self.was_transferred():
            return True
        return False

    def important_reason(self) -> str:
        reasons: list[str] = []
        if not self.was_transferred():
            reasons.append("created and not passed to the database")
        if self.updated_after_created():
            reasons.append("updated after it was created")
        if self.database_needs_update():
            reasons.append("updated after it was passed to the database")
        return "; ".join(reasons)

    def key(self) -> str:
        return self.scanner_id or self.isbn or self.danacode or self.url or self.display_title()

    def display_title(self) -> str:
        return self.title or self.title_en or self.title_phonetic or self.url

    def refresh_text_fields(self, *, allow_llm: bool = False) -> bool:
        """Fix garbled encodings and fill official English plus phonetic titles.

        Generating a phonetic title does not change the updated date.
        """
        from hebrew_text import repair_text, split_hebrew_latin

        self.title = repair_text(self.title)
        self.title_en = repair_text(self.title_en)
        self.title_phonetic = repair_text(self.title_phonetic)
        self.author = repair_text(self.author)
        self.publisher = repair_text(self.publisher)
        self.description = repair_text(self.description)
        self.translator = repair_text(self.translator)
        self.illustrator = repair_text(self.illustrator)
        hebrew, latin = split_hebrew_latin(self.title)
        if hebrew and latin:
            self.title = hebrew
            if not (self.title_en or "").strip():
                self.title_en = latin
        elif latin and not has_hebrew(self.title) and not (self.title_en or "").strip():
            self.title_en = latin
        captured = self.captured_fields()
        if not (self.title_en or "").strip():
            incoming = repair_text(captured.get("title_en") or "")
            if incoming and not has_hebrew(incoming):
                self.title_en = incoming
        phonetic = (self.title_phonetic or "").strip()
        if phonetic and has_hebrew(phonetic):
            phonetic = ""
        if not phonetic:
            captured_ph = repair_text(captured.get("title_phonetic") or "")
            if captured_ph and not has_hebrew(captured_ph):
                phonetic = captured_ph
        self.title_phonetic = phonetic
        return self.ensure_phonetic(allow_llm=allow_llm)

    def ensure_phonetic(self, *, allow_llm: bool = False) -> bool:
        """Fill a missing phonetic title from Hebrew. Does not change the updated date."""
        from hebrew_text import hebrew_phonetic, split_hebrew_latin

        current = (self.title_phonetic or "").strip()
        if current and not has_hebrew(current) and not allow_llm:
            return False
        if current and not has_hebrew(current) and (self.extra.get("phonetic_source") or "") == "llm":
            return False
        hebrew, _latin = split_hebrew_latin(self.title)
        source = hebrew if has_hebrew(hebrew) else (self.title if has_hebrew(self.title) else "")
        if not source:
            return False
        if allow_llm:
            import llm_client

            generated = llm_client.phonetic_title(source, allow_llm=True)
            if generated and not has_hebrew(generated) and generated != current:
                self.title_phonetic = generated
                self.extra["phonetic_source"] = (
                    "llm" if llm_client.last_phonetic_report.succeeded else "algorithm"
                )
                return True
            generated = generated or hebrew_phonetic(source)
        else:
            generated = hebrew_phonetic(source)
        if not generated or generated == current:
            return False
        self.title_phonetic = generated
        if not (self.extra.get("phonetic_source") or "").strip():
            self.extra["phonetic_source"] = "algorithm"
        return True

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

    def display_tone(self) -> str:
        if self.final:
            return "final"
        if self.approved:
            return "approved"
        if (self.scan_status or "") == "failed" or self.extra.get("lookup_error"):
            return "error"
        return ""

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
        current_en = str(self.title_en or "").strip()
        incoming_en = str(other.title_en or "").strip()
        if not current_en and incoming_en:
            self.title_en = incoming_en
            filled.append("title_en")
        current_ph = str(self.title_phonetic or "").strip()
        incoming_ph = str(other.title_phonetic or "").strip()
        if (not current_ph or has_hebrew(current_ph)) and incoming_ph and not has_hebrew(incoming_ph):
            self.title_phonetic = incoming_ph
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
                if name == "title_phonetic":
                    continue
                filled.append(name)
                self.record_field_source(name, other.field_source_url(name) or other.url)
        if captured:
            self._save_map("captured", captured)
        captured_ph = str(captured.get("title_phonetic") or "").strip()
        current_ph = str(self.title_phonetic or "").strip()
        if (not current_ph or has_hebrew(current_ph)) and captured_ph and not has_hebrew(captured_ph):
            self.title_phonetic = captured_ph
        self.ensure_phonetic()
        if other.extra.get("found_fields"):
            self.extra["publisher_found"] = other.extra["found_fields"]
        if other.extra.get("page_fields"):
            self.extra["page_fields"] = other.extra["page_fields"]
        filled = list(dict.fromkeys(filled))
        if filled:
            self.stamp_modified()
        return filled

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

    def mark_publisher_lookup(
        self,
        site: str,
        page: str = "",
        filled: list[str] | None = None,
        note: str = "",
        error: bool = False,
    ) -> None:
        if site:
            self.extra["publisher_site"] = site
        if page:
            self.extra["publisher_page"] = page
        self.extra["new_fields"] = ",".join(filled or [])
        self.extra["lookup_note"] = note
        if error:
            self.extra["lookup_error"] = "1"
        else:
            self.extra.pop("lookup_error", None)

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
        if (self.title_en or "").strip():
            title_en = self.title_en
        elif captured.get("title_en"):
            title_en = captured.get("title_en") or title_en
        if has_hebrew(self.title):
            title_he = self.title
        self.ensure_phonetic()
        phonetic = (self.title_phonetic or "").strip()
        fields = {
            "publisher": clean(self.publisher),
            "author_en": author_en,
            "title_en": title_en,
            "title_he": title_he,
            "title_phonetic": phonetic,
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
            "translator": format_person_name(self.translator) or clean(self.translator),
            "illustrator": format_person_name(self.illustrator) or clean(self.illustrator),
            "marc": clean(self.marc),
            "ddc": clean(self.ddc),
            "scanner_id": clean(self.scanner_id),
            "url": self.url,
            "created_at": clean(self.created_at),
            "modified_at": clean(self.modified_at),
            "database_passed_at": clean(self.database_passed_at),
        }
        if captured.get("author_en"):
            author_en = captured.get("author_en") or author_en
        if captured.get("author_he"):
            author_he = captured.get("author_he") or author_he
        fields["author_en"] = format_person_name(author_en, hebrew=False)
        fields["author_he"] = format_person_name(author_he, hebrew=True)
        legacy = captured.get("translated") or ""
        if not fields["translator"] and _looks_like_person_name(legacy) and len(legacy.split()) >= 2:
            fields["translator"] = format_person_name(legacy)
            fields["translated"] = "Y"
        elif captured.get("translated"):
            fields["translated"] = legacy if not _looks_like_person_name(legacy) else "Y"
        if fields["translator"] and not fields.get("translated"):
            fields["translated"] = "Y"
        for name, value in captured.items():
            if name in {"translated", "translator"}:
                continue
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
    new_names: int = 0
    from_cache: bool = False
    cancelled: bool = False
    error: str = ""
    error_books: int = 0
    site_notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.error and not self.matched:
            return f"Search failed: {self.error}"
        if self.from_cache:
            return f"From cache: {self.matched} book(s) loaded. No pages fetched."
        parts = [f"{self.matched:,} book(s) on the list"]
        if self.new_names:
            parts.append(f"{self.new_names:,} new this search")
        if self.product_links:
            parts.append(f"{self.product_links:,} listed from catalogs")
        if self.listing_pages:
            parts.append(f"{self.listing_pages} catalog page(s) read")
        if self.product_cached:
            parts.append(f"{self.product_cached} filled from cache")
        if self.product_fetched:
            parts.append(f"{self.product_fetched} product pages opened")
        if self.error_books:
            parts.append(f"{self.error_books} error books (Filter: Errors)")
        page_failed = self.listing_failed + self.product_failed
        if page_failed:
            parts.append(f"{page_failed} catalog pages that did not open")
        if self.skipped_year:
            parts.append(f"{self.skipped_year} wrong year")
        if self.enriched:
            parts.append(f"{self.enriched} filled from other sites")
        if self.cancelled:
            parts.append("stopped early")
        if self.site_notes:
            parts.append(" · ".join(self.site_notes))
        return "Search summary — " + ". ".join(parts) + "."


def has_hebrew(text: str | None) -> bool:
    return bool(HEBREW_RE.search(text or ""))


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return ""
    from hebrew_text import repair_text

    return repair_text(text)


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


def _format_one_person(name: str, hebrew: bool | None = None) -> str:
    value = clean(name)
    if not value:
        return ""
    if hebrew is None:
        hebrew = has_hebrew(value)
    if hebrew:
        if "," in value:
            family, given = (part.strip() for part in value.split(",", 1))
            if family and given:
                return f"{given} {family}"
        return value
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


def format_person_name(text: str | None, hebrew: bool | None = None) -> str:
    people = [_format_one_person(part, hebrew=hebrew) for part in _split_people(text or "")]
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
    short = re.sub(r"\D", "", book.danacode_short() or book.extra.get("danacode_short") or "")
    if short:
        keys.add(f"dana:{short}")
    title = normalize_name(book.title)
    author = normalize_name(book.author)
    if title and author:
        keys.add(f"ta:{title}|{author}")
    return keys


def authors_match(left: str, right: str) -> bool:
    left_name = normalize_name(left)
    right_name = normalize_name(right)
    if not left_name or not right_name:
        return False
    if left_name in right_name or right_name in left_name:
        return True
    left_tokens = set(left_name.split())
    right_tokens = set(right_name.split())
    if not left_tokens or not right_tokens:
        return False
    smaller, larger = (left_tokens, right_tokens) if len(left_tokens) <= len(right_tokens) else (right_tokens, left_tokens)
    return smaller <= larger


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
        return authors_match(left_author, right_author)
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


def absorb_book(primary: list[Book], extra: Book) -> tuple[Book, bool]:
    """Merge extra into the list, or append it. Returns (canonical book, added?)."""
    extra.refresh_text_fields()
    extra_title = (extra.title or "").strip()
    extra_url = (extra.url or "").strip()
    if extra_url:
        for book in primary:
            if (book.url or "").strip() == extra_url:
                _keep_identity(book, extra)
                book.merge_missing(extra)
                overlay_evrit_catalog(book, extra)
                book.refresh_text_fields()
                return book, False
    if extra_title:
        for book in primary:
            if books_match(book, extra):
                _keep_identity(book, extra)
                book.merge_missing(extra)
                overlay_evrit_catalog(book, extra)
                book.refresh_text_fields()
                return book, False
        extra.stamp_created()
        primary.append(extra)
        return extra, True
    if extra_url:
        extra.stamp_created()
        primary.append(extra)
        return extra, True
    return extra, False


def overlay_evrit_catalog(book: Book, extra: Book) -> None:
    """e-vrit catalog year, printed pages, and printed price replace weaker guesses."""
    if not is_evrit_host(extra.url):
        return
    if extra.year:
        book.year = extra.year
    if extra.pages:
        book.pages = extra.pages
    extra_price = format_price(extra.price_ils)
    if not extra_price:
        return
    current = format_price(book.price_ils)
    try:
        better = not current or float(extra_price) >= float(current or 0)
    except ValueError:
        better = not current
    if better:
        book.price_ils = extra.price_ils


def fill_missing_entry_dates(books: list[Book], when: str = "") -> int:
    """Stamp every book that is missing created or updated, all with the same time."""
    stamp = (when or "").strip() or entry_now()
    filled = 0
    for book in books:
        if book.fill_missing_dates(stamp):
            filled += 1
    return filled


def fill_missing_phonetics(books: list[Book], *, use_llm: bool = True) -> int:
    """Generate phonetic titles for Hebrew books that lack one. Does not change updated dates."""
    from hebrew_text import has_hebrew as title_has_hebrew, split_hebrew_latin
    from llm_client import PhoneticLlmReport, llm_allowed, phonetic_titles
    import llm_client

    llm_client.last_phonetic_report = PhoneticLlmReport()
    filled = 0
    pending: list[Book] = []
    sources: list[str] = []
    for book in books:
        if book.refresh_text_fields(allow_llm=False):
            filled += 1
        hebrew, _latin = split_hebrew_latin(book.title)
        source = hebrew if title_has_hebrew(hebrew) else (book.title if title_has_hebrew(book.title) else "")
        if source and (book.extra.get("phonetic_source") or "") != "llm":
            pending.append(book)
            sources.append(source)
    if pending and use_llm and llm_allowed():
        results = phonetic_titles(sources, allow_llm=True)
        llm_set = set(llm_client.last_phonetic_report.llm_indexes)
        for offset, book in enumerate(pending):
            text = results[offset] if offset < len(results) else ""
            if not text or title_has_hebrew(text):
                continue
            was_missing = not (book.title_phonetic or "").strip() or title_has_hebrew(book.title_phonetic)
            book.title_phonetic = text
            if offset in llm_set:
                book.extra["phonetic_source"] = "llm"
            elif was_missing and not (book.extra.get("phonetic_source") or "").strip():
                book.extra["phonetic_source"] = "algorithm"
            if was_missing:
                filled += 1
    return filled


def _keep_identity(book: Book, extra: Book) -> None:
    if extra.scanner_id and not book.scanner_id:
        book.scanner_id = extra.scanner_id
    extra_created = (extra.created_at or "").strip()
    book_created = (book.created_at or "").strip()
    if extra_created and (not book_created or extra_created < book_created):
        book.created_at = extra.created_at
    if not (book.modified_at or "").strip() and (extra.modified_at or "").strip():
        book.modified_at = extra.modified_at
    if extra.database_passed_at and not book.database_passed_at:
        book.database_passed_at = extra.database_passed_at
    if extra.excel_passed:
        book.excel_passed = True
    if extra.approved:
        book.approved = True
    if extra.final:
        book.final = True
    book.stamp_created()


def union_catalog(primary: list[Book], extras: list[Book]) -> int:
    """Merge matching books, and append titles that are not already in the list."""
    added = 0
    for extra in extras:
        _canonical, is_new = absorb_book(primary, extra)
        if is_new:
            added += 1
    return added


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


def plausible_ils(value: str) -> str:
    formatted = format_price(value) or parse_price(value)
    try:
        amount = float(formatted)
    except (TypeError, ValueError):
        return ""
    if amount < 3 or amount > 2500:
        return ""
    return formatted


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
    if is_evrit_host(book.url):
        return
    catalog = ""
    for block in soup.select(".price--on-sale .price__sale, .price__sale"):
        struck = block.select_one("s, del, strike")
        if struck:
            catalog = parse_price(struck.get_text(" ", strip=True))
            catalog = plausible_ils(catalog)
            if catalog:
                break
    if not catalog:
        for node in soup.find_all(string=re.compile(r"מחיר\s*קטלוגי|compare[-_ ]?at", re.I)):
            parent = getattr(node, "parent", None)
            if parent is None:
                continue
            container = parent.parent if parent.parent else parent
            catalog = plausible_ils(parse_price(container.get_text(" ", strip=True)))
            if catalog:
                break
    if not catalog:
        regular = soup.select_one(".special_price, .oldprice, .label-price")
        if regular and not regular.find_parent(class_=["list_product", "grid-products", "product-cube"]):
            catalog = plausible_ils(regular.get_text(" ", strip=True))
        if not catalog:
            for node in soup.find_all(string=re.compile(r"מחיר\s*רגיל", re.I)):
                parent = getattr(node, "parent", None)
                if parent is None:
                    continue
                catalog = plausible_ils(parent.get_text(" ", strip=True))
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


def fill_from_schema(book: Book, item: dict, url: str = "") -> None:
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
    if isinstance(offers, dict) and not is_evrit_host(url):
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
    if not book.translator:
        book.translator = schema_name(item.get("translator"))
    if not book.illustrator:
        book.illustrator = schema_name(item.get("illustrator") or item.get("artist"))


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
    for block in soup.select(
        ".display-element, prm-data-field, .item-details-element, .full-view-field, [class*='display-element']"
    ):
        if not isinstance(block, Tag):
            continue
        title = block.select_one(
            ".display-element-title, .data-field-title, .full-view-label, dt, .label, span[class*='title']"
        )
        value = block.select_one(
            ".display-element-text, .data-field-value, .full-view-value, dd, .value, span[class*='text']"
        )
        if title and value:
            remember(title.get_text(" ", strip=True), value.get_text(" ", strip=True))
    for row in soup.select("li"):
        if not isinstance(row, Tag):
            continue
        title = row.select_one(".titleBullet, .title-bullet")
        value = row.select_one(".valBullet, .val-bullet")
        if title and value:
            remember(title.get_text(" ", strip=True), value.get_text(" ", strip=True))
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
    translators = [a.get_text(" ", strip=True) for a in root.select("[itemprop='translator']")]
    if translators and not book.translator:
        book.translator = format_person_name(", ".join(dict.fromkeys(translators))) or ", ".join(dict.fromkeys(translators))
    illustrators = [
        a.get_text(" ", strip=True) for a in root.select("[itemprop='illustrator'], [itemprop='artist']")
    ]
    if illustrators and not book.illustrator:
        book.illustrator = format_person_name(", ".join(dict.fromkeys(illustrators))) or ", ".join(
            dict.fromkeys(illustrators)
        )
    publisher = root.select_one("#product-page-manufacturer-name, [itemprop='publisher']")
    if publisher:
        book.publisher = book.publisher or publisher.get_text(" ", strip=True)
    price = root.select_one("#product-page-price")
    if price:
        book.price_ils = book.price_ils or price.get("data-price") or parse_price(price.get_text(" ", strip=True))
    printed = root.select_one(".itemPrice ins, #itemPrice ins, .price ins")
    if printed:
        printed_price = parse_price(printed.get_text(" ", strip=True))
        if printed_price:
            book.price_ils = printed_price
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


def fill_from_nli(book: Book, soup: BeautifulSoup, url: str) -> None:
    if not is_nli_host(url):
        return
    text = soup.get_text("\n", strip=True)
    title = soup.select_one(
        "h1, .item-title, .full-view-title, prm-brief-result h3, [data-field='title']"
    )
    if title:
        book.title = book.title or clean(title.get_text(" ", strip=True))
    creator = soup.select_one("[data-field='creator'], .item-detail-creator, prm-brief-result .author")
    if creator:
        book.author = book.author or format_person_name(creator.get_text(" ", strip=True)) or clean(
            creator.get_text(" ", strip=True)
        )
    if not book.ddc:
        match = re.search(
            r"(?:dewey(?:\s+(?:decimal|class(?:ification| number)?))?|ddc|דיואי(?:\s+עשרוני)?|סיווג\s*דיואי)\s*[:\-]?\s*([0-9]{1,3}(?:\.[0-9]+)*)",
            text,
            re.I,
        )
        if match:
            book.ddc = match.group(1)
    if not book.marc:
        match = re.search(
            r"(?:mms\s*id|system\s*number|מספר\s*מערכת|marc(?:\s*21)?)\s*[:\-]?\s*(\d{6,})",
            text,
            re.I,
        )
        if match:
            book.marc = match.group(1)
    if not book.marc:
        parsed = urlparse(url)
        for key, values in parse_qs(parsed.query).items():
            if key.casefold() in {"docid", "doc_id", "mms_id", "recordid", "record_id"} and values:
                digits = re.sub(r"\D", "", values[0])
                if len(digits) >= 6:
                    book.marc = digits
                    break
    if not book.isbn:
        for match in ISBN_RE.finditer(text[:8000]):
            apply_identifier(book, match.group(0))
            if book.isbn:
                break


def shopify_money(raw: Any, cents: bool | None = None) -> str:
    if raw is None or raw == "":
        return ""
    text = str(raw).strip().replace(",", "")
    if not text:
        return ""
    dotted = "." in text
    try:
        amount = float(text)
    except ValueError:
        return parse_price(text)
    use_cents = cents if cents is not None else (not dotted and amount >= 1000 and amount == int(amount))
    if use_cents:
        amount = amount / 100.0
    return format_price(amount)


def fill_from_shopify_payload(book: Book, item: dict[str, Any], origin: str = "", cents: bool | None = None) -> None:
    """Map a Shopify product or collection JSON object onto a book."""
    if not isinstance(item, dict):
        return
    title = clean(item.get("title") or item.get("name") or "")
    if title:
        book.title = book.title or title
    handle = str(item.get("handle") or "").strip().strip("/")
    if origin and handle:
        book.url = book.url or f"{origin.rstrip('/')}/products/{handle.split('/')[-1]}"
    vendor = clean(item.get("vendor") or "")
    host_publisher = default_publisher_for_host(book.url or origin)
    if vendor:
        vendor_norm = normalize_name(vendor)
        publisher_like = any(word in vendor_norm for word in ("הוצאה", "ספרים", "publish", "books"))
        if publisher_like:
            book.publisher = book.publisher or vendor
        else:
            book.author = book.author or vendor
    if host_publisher:
        book.publisher = book.publisher or host_publisher
    published = str(item.get("published_at") or item.get("created_at") or "")
    if published and not book.year:
        book.year = extract_year(published)
    body = _plain_markup_text(item.get("body_html") or item.get("description") or "")
    if body:
        book.description = book.description or body
        book.pages = book.pages or parse_pages(body)
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    for tag in tags:
        text = clean(str(tag))
        if not text:
            continue
        apply_identifier(book, text)
        year = extract_year(text)
        if year and not book.year:
            book.year = year
        if not book.publisher and any(word in normalize_name(text) for word in ("הוצאה", "ספרים")):
            book.publisher = text
    variants = item.get("variants") if isinstance(item.get("variants"), list) else []
    variant = variants[0] if variants and isinstance(variants[0], dict) else {}
    apply_identifier(book, str(variant.get("barcode") or item.get("barcode") or ""))
    apply_identifier(book, str(variant.get("sku") or item.get("sku") or ""))
    compare = shopify_money(variant.get("compare_at_price") or item.get("compare_at_price"), cents)
    price = shopify_money(variant.get("price") or item.get("price"), cents)
    book.price_ils = compare or price or book.price_ils
    images = item.get("images") if isinstance(item.get("images"), list) else []
    image = ""
    if images:
        first = images[0]
        if isinstance(first, dict):
            image = str(first.get("src") or first.get("url") or "")
        else:
            image = str(first)
    image = image or str(item.get("featured_image") or "")
    if image and not book.cover_image_url:
        book.cover_image_url = image.split("?")[0]


def fill_from_bsmart(book: Book, soup: BeautifulSoup, url: str) -> None:
    """Publisher shops such as Modan, Keter, and Am Oved share this catalog HTML."""
    if not soup.select_one(".saleprice, .list_product, .grid-products, .titleBullet, .autor-lang, .authorName"):
        return
    if is_booknet_host(url) or is_evrit_host(url) or is_nli_host(url):
        return
    title = soup.select_one("h1")
    if title:
        book.title = book.title or clean(re.sub(r"\s+\|.*$", "", title.get_text(" ", strip=True)))
    authors = [
        clean(tag.get_text(" ", strip=True)).strip(" |")
        for tag in soup.select(".autor-lang, p.authorName, .authorName")
    ]
    authors = [name for name in authors if name and name.casefold() not in {book.title.casefold(), "מאפיינים"}]
    if authors and not book.author:
        book.author = format_person_name(authors[0]) or authors[0]
    host_publisher = default_publisher_for_host(url)
    if host_publisher:
        book.publisher = book.publisher or host_publisher
    catalog = ""
    sale_price = ""
    for node in soup.select(".saleprice-block, .special_price, .oldprice, .saleprice"):
        if node.find_parent(class_=["list_product", "grid-products", "product-cube"]):
            continue
        amount = plausible_ils(node.get_text(" ", strip=True))
        classes = " ".join(node.get("class") or [])
        if "saleprice-block" in classes or "special_price" in classes or "oldprice" in classes:
            catalog = catalog or amount
        else:
            sale_price = sale_price or amount
    if catalog:
        book.price_ils = catalog
    elif sale_price:
        book.price_ils = book.price_ils or sale_price
    if book.price_ils and not plausible_ils(book.price_ils):
        book.price_ils = ""
    for row in soup.select("li"):
        label = row.select_one(".titleBullet")
        value = row.select_one(".valBullet")
        if label and value:
            fill_from_labels(book, {label.get_text(" ", strip=True): value.get_text(" ", strip=True)})
    if not book.year or not book.pages:
        for item in soup.select("li"):
            text = item.get_text(" ", strip=True)
            if "שנת הוצאה" in text and not book.year:
                book.year = extract_year(text)
            if ("עמוד" in text or "עמ'" in text) and not book.pages:
                book.pages = parse_pages(text)
    bst = soup.select_one("[data-bst]")
    if bst:
        apply_identifier(book, str(bst.get("data-bst") or ""))
    cover = soup.find(string=re.compile(r"סוג כריכה"))
    if cover and not book.cover_type:
        parent = getattr(cover, "parent", None)
        if parent is not None:
            book.cover_type = map_cover(parent.get_text(" ", strip=True))


def extract_book_from_html(html: str, url: str) -> Book:
    soup = parse_html(html)
    book = Book(url=url)
    for item in json_ld_objects(html):
        fill_from_schema(book, item, url)
    fill_from_booknet(book, soup, url)
    fill_from_magento(book, soup)
    fill_from_bsmart(book, soup, url)
    from field_map import attach_page_fields, collect_extra_pairs, remember_candidates

    pairs = labeled_value_pairs(soup)
    pairs.update(collect_extra_pairs(soup, html))
    fill_from_labels(book, pairs)
    attach_page_fields(book, pairs)
    remember_candidates(pairs, url)
    fill_from_nli(book, soup, url)
    fill_from_evrit_html(book, soup, url)
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
    if book.price_ils and not plausible_ils(book.price_ils):
        book.price_ils = ""
    book.year = extract_year(book.year)
    book.title = clean(book.title)
    book.author = format_person_name(book.author) or clean(book.author)
    book.translator = format_person_name(book.translator) or clean(book.translator)
    book.illustrator = format_person_name(book.illustrator) or clean(book.illustrator)
    book.publisher = clean(book.publisher)
    book.description = clean(book.description)
    fill_book_images(book, soup, url, html)
    book.refresh_text_fields()
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
    if soup.select_one(".list_product, .grid-products, .product-cube, body.product_list"):
        return False
    if soup.select_one("h1") and soup.select_one(".saleprice, .titleBullet, .autor-lang"):
        return True
    path = urlparse(url).path.lower()
    return any(hint in path for hint in ("/מוצרים/", "/product/", "/products/", "/product-page/", "/book/", "/page_"))


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


def is_asset_url(url: str) -> bool:
    """Cover images and other static files are not book pages."""
    path = unquote(urlparse(url or "").path).casefold()
    return path.endswith(ASSET_SUFFIXES)


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
        if not url or not same_domain(page_url, url) or is_asset_url(url):
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


def collect_product_entries(soup: BeautifulSoup, page_url: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(href: str | None, title: str = "") -> None:
        url = normalize_url(page_url, href or "")
        if not url or "/icons/" in url or is_asset_url(url) or url in seen:
            return
        path = urlparse(url).path.lower()
        if not any(hint in path for hint in PRODUCT_PATH_HINTS):
            return
        seen.add(url)
        found.append((url, clean(title)))

    def title_from(tag: Tag) -> str:
        text = clean(tag.get_text(" ", strip=True))
        if len(text) >= 2:
            return text[:180]
        img = tag.find("img") if isinstance(tag, Tag) else None
        alt = clean(img.get("alt") if img else "")
        return alt[:180]

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
            if isinstance(tag, Tag):
                add(tag.get("href", ""), title_from(tag))
    if found:
        return found
    html = str(soup)
    for match in PRODUCT_HREF_RE.finditer(html):
        add(match.group(1), "")
    if found:
        return found
    for tag in soup.select("a[href]"):
        if not isinstance(tag, Tag):
            continue
        href = tag.get("href", "")
        path = urlparse(urljoin(page_url, href)).path
        if any(hint in path for hint in PRODUCT_PATH_HINTS) and "/קטגוריות/" not in path:
            add(href, title_from(tag))
    return found


def collect_product_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    return [url for url, _title in collect_product_entries(soup, page_url)]


_LISTING_SKIP_TITLES = {
    "לפרטים נוספים",
    "הוסף לסל",
    "הוספה למועדפים",
    "לכל הספרים",
    "לרשימת הספרים המלאה",
}


def _bsmart_price_index(html: str) -> dict[str, tuple[str, str]]:
    """id -> (list price, barcode)."""
    found: dict[str, tuple[str, str]] = {}
    match = re.search(r"var idObjects = '(\[.*?\])'", html or "", re.S)
    if not match:
        return found
    try:
        rows = json.loads(match.group(1))
    except json.JSONDecodeError:
        return found
    if not isinstance(rows, list):
        return found
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id") or "").strip()
        if not item_id:
            continue
        price = format_price(row.get("bsmartPrice"))
        code = ""
        codes = row.get("codes")
        if isinstance(codes, list):
            for entry in codes:
                if isinstance(entry, dict) and entry.get("code"):
                    code = str(entry.get("code") or "")
                    break
        found[item_id] = (price, code)
    return found


def _card_bsmart_id(card: Tag) -> str:
    for tag in card.select("[id]"):
        match = re.search(r"(?:stock|saleprice|oldprice|crntPrice)(\d+)$", str(tag.get("id") or ""))
        if match:
            return match.group(1)
    match = re.search(r"currentID\s*=\s*'(\d+)'", str(card))
    return match.group(1) if match else ""


def _listing_link(card: Tag, page_url: str) -> str:
    skip_bits = ("/מבצעים/", "/מחברים/", "/הוצאות/", "/קטגוריות/", "/authors/", "/collections/")
    fallback = ""
    for tag in card.select("a[href]"):
        href = str(tag.get("href") or "")
        if href.startswith("javascript") or href in {"#", "/"}:
            continue
        url = normalize_url(page_url, href)
        if not url or is_asset_url(url):
            continue
        path = unquote(urlparse(url).path)
        if unquote(urlparse(page_url).path).rstrip("/") == path.rstrip("/"):
            continue
        if any(bit in path for bit in skip_bits):
            continue
        if any(hint in path.lower() or hint in path for hint in PRODUCT_PATH_HINTS):
            return url
        if not fallback:
            fallback = url
    return fallback


def collect_booknet_listings(soup: BeautifulSoup, page_url: str) -> list[Book]:
    books: list[Book] = []
    seen: set[str] = set()
    for card in soup.select(".product-cube, .products.product-cube"):
        if not isinstance(card, Tag):
            continue
        url = _listing_link(card, page_url)
        if not url or "/מוצרים/" not in unquote(urlparse(url).path) or url in seen:
            continue
        seen.add(url)
        title = clean(card.get("data-fullname") or card.get("data-fullName") or "")
        if not title:
            heading = card.select_one("h3.productTitle, .productTitle")
            title = clean(heading.get_text(" ", strip=True) if heading else "")
        if not title or title in _LISTING_SKIP_TITLES:
            continue
        book = Book(url=url, title=title)
        book.publisher = clean(card.get("data-manufacturer") or "")
        author = card.select_one(".product-author, .book-below-title")
        if author:
            book.author = format_person_name(author.get_text(" ", strip=True)) or clean(author.get_text(" ", strip=True))
        price_node = card.select_one(".price ins, ins")
        if price_node:
            book.price_ils = parse_price(price_node.get_text(" ", strip=True))
        img = card.select_one("img[data-original], img[src]")
        src = ""
        if img:
            src = str(img.get("data-original") or img.get("src") or "")
            alt = clean(img.get("alt") or "")
            if alt and not book.title:
                book.title = alt
            if src.startswith("/"):
                book.cover_image_url = urljoin(page_url, src)
            elif src.startswith("http"):
                book.cover_image_url = src
        apply_identifier(book, url)
        apply_identifier(book, src)
        book.refresh_text_fields()
        books.append(book)
    return books


def collect_bsmart_listings(soup: BeautifulSoup, page_url: str) -> list[Book]:
    cards = [tag for tag in soup.select(".list_product") if isinstance(tag, Tag)]
    if not cards:
        cards = [tag for tag in soup.select(".grid-products") if isinstance(tag, Tag)]
    if not cards:
        cards = [tag for tag in soup.select(".products .item") if isinstance(tag, Tag)]
    if not cards:
        return []
    prices = _bsmart_price_index(str(soup))
    books: list[Book] = []
    seen: set[str] = set()
    host_publisher = default_publisher_for_host(page_url)
    for card in cards:
        url = _listing_link(card, page_url)
        if not url or url in seen:
            continue
        title = clean(card.get("data-title") or "")
        if not title:
            heading = card.select_one("h2, h3, .description h2")
            title = clean(heading.get_text(" ", strip=True) if heading else "")
        if not title:
            img = card.select_one("img[alt]")
            alt = clean(img.get("alt") if img else "")
            title = alt.split("|")[0].strip() if alt else ""
        if not title or title in _LISTING_SKIP_TITLES or len(title) < 2:
            continue
        seen.add(url)
        book = Book(url=url, title=title)
        author = clean(card.get("data-brand") or "")
        if not author:
            name = card.select_one(".authorName, .description > div")
            if name:
                author = clean(name.get_text(" ", strip=True))
        if author and author.casefold() != title.casefold():
            book.author = format_person_name(author) or author
        if host_publisher:
            book.publisher = host_publisher
        apply_identifier(book, str(card.get("data-bst") or ""))
        item_id = _card_bsmart_id(card)
        if item_id and item_id in prices:
            price, code = prices[item_id]
            if price:
                book.price_ils = price
            apply_identifier(book, code)
        if not book.price_ils:
            for sel in (".special_price", ".oldprice", ".saleprice"):
                node = card.select_one(sel)
                if node:
                    book.price_ils = parse_price(node.get_text(" ", strip=True))
                    if book.price_ils:
                        break
        img = card.select_one("img[data-src], img[src]")
        if img:
            src = str(img.get("data-src") or img.get("src") or "")
            if src.startswith("/"):
                book.cover_image_url = urljoin(page_url, src)
            elif src.startswith("http"):
                book.cover_image_url = src
        book.refresh_text_fields()
        books.append(book)
    return books


def collect_magento_listings(soup: BeautifulSoup, page_url: str) -> list[Book]:
    books: list[Book] = []
    seen: set[str] = set()
    for card in soup.select(".product-item-info, li.product-item"):
        if not isinstance(card, Tag):
            continue
        link = card.select_one("a.product-item-link, a.product-item-photo")
        url = normalize_url(page_url, link.get("href") if link else "")
        if not url or url in seen:
            continue
        title = clean(link.get_text(" ", strip=True) if link else "")
        if not title:
            img = card.select_one("img")
            title = clean(img.get("alt") if img else "")
        if not title:
            continue
        seen.add(url)
        book = Book(url=url, title=title)
        price = card.select_one("[data-price-amount], .price-wrapper .price")
        if price:
            book.price_ils = price.get("data-price-amount") or parse_price(price.get_text(" ", strip=True))
        book.publisher = book.publisher or default_publisher_for_host(page_url)
        book.refresh_text_fields()
        books.append(book)
    return books


def collect_nli_listings(soup: BeautifulSoup, page_url: str) -> list[Book]:
    if not is_nli_host(page_url):
        return []
    books: list[Book] = []
    seen: set[str] = set()
    for card in soup.select(
        "prm-brief-result, .result-item, .item-brief, article.result, .search-result-item, md-list-item"
    ):
        if not isinstance(card, Tag):
            continue
        link = card.select_one("a[href*='fulldisplay'], a[href*='docid'], a[href*='/books/'], h3 a[href], .item-title a")
        url = _listing_link(card, page_url) if not link else normalize_url(page_url, str(link.get("href") or ""))
        if not url or url in seen:
            continue
        title_el = card.select_one("h3, .item-title, .media-title, [data-field='title']")
        title = clean(title_el.get_text(" ", strip=True) if title_el else "")
        if not title:
            continue
        seen.add(url)
        book = Book(url=url, title=title)
        author_el = card.select_one(".author, [data-field='creator'], .item-detail-creator")
        if author_el:
            book.author = format_person_name(author_el.get_text(" ", strip=True)) or clean(
                author_el.get_text(" ", strip=True)
            )
        year_el = card.select_one("[data-field='date'], .date")
        if year_el:
            book.year = extract_year(year_el.get_text(" ", strip=True))
        fill_from_nli(book, card, url)
        book.refresh_text_fields()
        books.append(book)
    return books


def collect_listing_books(soup: BeautifulSoup, page_url: str) -> list[Book]:
    """Site-specific listing cards with title, author, price, and identifiers."""
    if is_booknet_host(page_url) or soup.select_one(".product-cube"):
        found = collect_booknet_listings(soup, page_url)
        if found:
            return found
    if is_nli_host(page_url):
        found = collect_nli_listings(soup, page_url)
        if found:
            return found
    found = collect_bsmart_listings(soup, page_url)
    if found:
        return found
    found = collect_magento_listings(soup, page_url)
    if found:
        return found
    return []


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


def _looks_like_cover_image(img: Tag) -> bool:
    if not isinstance(img, Tag) or (img.name or "").lower() != "img":
        return False
    alt = clean(img.get("alt") or "")
    src = " ".join(
        str(img.get(key) or "")
        for key in ("src", "data-src", "data-original", "srcset")
    ).casefold()
    blob = f"{alt} {src}"
    skip = ("logo", "icon", "sprite", "placeholder", "facebook", "whatsapp", "cart", "wishlist", "account", "pixel", "avatar", "badge")
    if any(bit in blob for bit in skip):
        return False
    if alt and len(alt) >= 2:
        return True
    classes = " ".join(img.get("class") or []).casefold()
    parent_classes = " ".join((img.parent.get("class") if img.parent else []) or []).casefold()
    return any(hint in classes or hint in parent_classes for hint in ("product", "card__media", "media", "book", "cover"))


def product_link_from_cover_image(img: Tag, page_url: str) -> tuple[str, str] | None:
    """On a ?q= picture grid, the cover is often not itself a link — the product link is on the card."""
    alt = clean(img.get("alt") or "")
    node: Tag | None = img
    for _ in range(10):
        parent = getattr(node, "parent", None)
        if not isinstance(parent, Tag):
            break
        node = parent
        name = (node.name or "").casefold()
        if name in {"body", "html", "ul", "ol", "main", "nav", "header", "footer"}:
            break
        for tag in node.select("a[href]"):
            url = normalize_url(page_url, tag.get("href") or "")
            if not url or is_asset_url(url) or is_query_listing_url(url) or not same_domain(page_url, url):
                continue
            path = unquote(urlparse(url).path)
            if not any(hint in path.lower() or hint in path for hint in PRODUCT_PATH_HINTS):
                continue
            if path.rstrip("/") in {"", "/"}:
                continue
            title = clean(tag.get_text(" ", strip=True)) or alt
            if title or path:
                return url, title
    parent_a = img.find_parent("a")
    if parent_a:
        url = normalize_url(page_url, parent_a.get("href") or "")
        path = unquote(urlparse(url or "").path)
        if (
            url
            and not is_asset_url(url)
            and not is_query_listing_url(url)
            and same_domain(page_url, url)
            and any(hint in path.lower() or hint in path for hint in PRODUCT_PATH_HINTS)
            and path.rstrip("/") not in {"", "/"}
        ):
            return url, alt
    return None


def collect_cover_picture_links(soup: BeautifulSoup, page_url: str) -> list[tuple[str, str]]:
    """Product pages reached by clicking book pictures on a search/query listing."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        if not _looks_like_cover_image(img):
            continue
        hit = product_link_from_cover_image(img, page_url)
        if not hit:
            continue
        url, title = hit
        key = _url_key(url)
        if key in seen:
            if title:
                previous = next((item for item in found if _url_key(item[0]) == key), None)
                if previous and len(title) > len(previous[1]):
                    found[found.index(previous)] = (url, title)
            continue
        seen.add(key)
        found.append((url, title))
    return found


def collect_search_result_links(soup: BeautifulSoup, page_url: str, book: Book) -> list[str]:
    """Product links on a ?q= search page, ranked toward the matching series volume."""
    current = _url_key(page_url)
    cards: dict[str, tuple[str, str]] = {}

    def add(href: str | None, title_text: str = "") -> None:
        url = normalize_url(page_url, href or "")
        if not url or not same_domain(page_url, url) or is_query_listing_url(url) or is_asset_url(url):
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
    for url, title_text in collect_cover_picture_links(soup, page_url):
        add(url, title_text)
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
        if not url or not same_domain(page_url, url) or is_asset_url(url):
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
        hit = product_link_from_cover_image(img, page_url) if _looks_like_cover_image(img) else None
        if hit:
            add(hit[0], image=True)
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


def listed_page_number(url: str) -> int:
    parsed = urlparse(url or "")
    qs = parse_qs(parsed.query)
    for key in LISTING_PAGE_QUERY_KEYS:
        raw = (qs.get(key) or [""])[0]
        if str(raw).isdigit():
            return int(raw)
    match = PAGE_IN_URL_RE.search(url or "")
    if match:
        return int(match.group(1))
    return 0


def page_number(url: str) -> int:
    return listed_page_number(url) or 1


def listing_page_cap(max_listing_pages: int) -> int:
    try:
        value = int(max_listing_pages)
    except (TypeError, ValueError):
        value = CATALOG_MIN_LISTING_PAGES
    if value <= 0:
        return UNLIMITED_LISTING_PAGES
    return max(1, value)


def last_listing_page(soup: BeautifulSoup, page_url: str) -> int:
    current = page_number(page_url)
    last = 0

    def consider(number: int) -> None:
        nonlocal last
        if number > last:
            last = number

    for tag in soup.select(
        ".pagination a[href], .pages a[href], .pager a[href], nav.pagination a[href], "
        "a[rel='last'], a[rel='next'], a.page-next, a.num, link[rel='next'], "
        ".pagination option, .pages option, .pager option, select[name*='page'] option"
    ):
        href = str(tag.get("href") or tag.get("value") or "")
        text = re.sub(r"[^\d]", "", tag.get_text(" ", strip=True) or "")
        number = 0
        if href:
            if href.isdigit():
                number = int(href)
            else:
                resolved = normalize_url(page_url, href)
                if resolved:
                    number = listed_page_number(resolved)
        if not number and text.isdigit():
            number = int(text)
        consider(number)
    for box in soup.select(".pagination, .pages, .pager, nav.pagination, .paging"):
        text = box.get_text(" ", strip=True)
        match = re.search(r"(?:מתוך|of|/)\s*(\d{1,5})\b", text, re.I)
        if match:
            consider(int(match.group(1)))
    if last <= current:
        return 0
    return last


def json_item_count(payload: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        return 0
    for key in ("Total", "total", "TotalItems", "TotalCount", "Count", "count", "ItemsCount"):
        raw = payload.get(key)
        if isinstance(raw, int) and raw >= 0:
            return raw
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw.strip())
    nested = payload.get("Paging") or payload.get("paging") or payload.get("Meta") or payload.get("meta")
    if isinstance(nested, dict):
        for key in ("Total", "total", "TotalItems", "TotalCount", "Count", "count"):
            raw = nested.get(key)
            if isinstance(raw, int) and raw >= 0:
                return raw
            if isinstance(raw, str) and raw.strip().isdigit():
                return int(raw.strip())
    return 0


def listing_page_query_key(url: str) -> str:
    qs = parse_qs(urlparse(url or "").query)
    for key in LISTING_PAGE_QUERY_KEYS:
        if key in qs:
            return key
    return "page"


def sequential_listing_urls(page_url: str, max_pages: int, page_key: str = "") -> list[str]:
    parsed = urlparse(page_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    key = page_key or listing_page_query_key(page_url)
    current = page_number(page_url)
    urls: list[str] = []
    drop = {key, "page", "p", "pg", "bscrp"}
    for number in range(1, max_pages + 1):
        if number == current:
            continue
        items = [
            (name, value)
            for name, values in qs.items()
            if name not in drop and name != ""
            for value in values
            if value
        ]
        items.append((key, str(number)))
        urls.append(urlunparse(parsed._replace(query=urlencode(items, safe="/"))))
    return urls


def collect_pagination_links(soup: BeautifulSoup, page_url: str, max_pages: int = 40) -> list[str]:
    links: list[str] = []
    start = urlparse(page_url)
    page_key = listing_page_query_key(page_url)
    for tag in soup.select(
        ".pagination a[href], .pages a[href], a[rel='next'], a.page-next, a.num, link[rel='next']"
    ):
        url = normalize_url(page_url, tag.get("href", ""))
        if not url:
            continue
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        same_list = unquote(parsed.path).rstrip("/") == unquote(start.path).rstrip("/")
        if not same_list and not any(key in qs for key in LISTING_PAGE_QUERY_KEYS):
            continue
        for key in LISTING_PAGE_QUERY_KEYS:
            if key in qs:
                page_key = key
                break
        if url not in links:
            links.append(url)
    if not links:
        return links
    last = last_listing_page(soup, page_url)
    cap = listing_page_cap(max_pages)
    if last > 1:
        cap = min(cap, last)
    return sequential_listing_urls(page_url, max(1, cap), page_key=page_key)


class BookCrawler:
    def __init__(
        self,
        delay_seconds: float = 0.35,
        timeout: int = 25,
        cancelled: Callable[[], bool] | None = None,
        progress: ProgressFn | None = None,
        event: EventFn | None = None,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.cancelled = cancelled or (lambda: False)
        self.progress = progress or (lambda _msg: None)
        self.event = event or (lambda _kind, _data: None)
        self.report = CrawlReport()
        self.last_site_error = ""
        self.session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.4, status_forcelist=(429, 502, 503, 504))
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

    def _emit(self, kind: str, data: dict[str, Any] | None = None) -> None:
        try:
            self.event(kind, data or {})
        except Exception:
            pass

    def _take_book(self, books: list[Book], book: Book, site: str, url: str) -> None:
        canonical, is_new = absorb_book(books, book)
        titled = sum(1 for item in books if (item.title or "").strip())
        self._emit(
            "book",
            {
                "book": canonical,
                "new": is_new,
                "found": titled,
                "site": site,
                "url": url,
            },
        )

    def fetch(self, url: str) -> tuple[str, str]:
        self._check_cancel()
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SiteError(request_failure_message(exc, url), url=url) from exc
        html = decode_http_text(response)
        failure = site_error_message(html, response.status_code, response.url or url)
        if failure:
            raise SiteError(failure, url=response.url or url)
        if response.status_code >= 400:
            raise SiteError(http_status_message(response.status_code, response.url or url), url=response.url or url)
        path_l = urlparse(url).path.lower()
        if "/api/" not in path_l and not path_l.endswith(".js") and not path_l.endswith("products.json"):
            time.sleep(self.delay_seconds)
        return html, response.url

    def _evrit_json(self, url: str) -> dict[str, Any] | None:
        try:
            raw, _final = self.fetch(url)
        except (SiteError, requests.RequestException):
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def apply_evrit_product_apis(self, book: Book, page_url: str) -> None:
        """Fill year, printed pages, and printed price from e-vrit JSON APIs."""
        product_id = evrit_product_id(page_url or book.url)
        if not product_id:
            return
        parsed = urlparse(page_url if "://" in (page_url or "") else "https://" + (page_url or book.url))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        for path in (
            f"/api/product/{product_id}",
            f"/api/product/extra/{product_id}",
            f"/api/product/extra/{product_id}/2",
        ):
            payload = self._evrit_json(origin + path)
            if payload:
                fill_from_evrit_payload(book, payload)
        book.refresh_text_fields()
        book.mark_origin_fields()

    def _load_json(self, url: str) -> Any:
        try:
            raw, _final = self.fetch(url)
        except (SiteError, requests.RequestException):
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def apply_shopify_product_json(self, book: Book, page_url: str) -> None:
        parsed = urlparse(page_url if "://" in (page_url or "") else "https://" + (page_url or book.url or ""))
        path = unquote(parsed.path).rstrip("/")
        if "/products/" not in path.lower():
            return
        origin = f"{parsed.scheme}://{parsed.netloc}"
        js_url = page_url if path.lower().endswith(".js") else f"{origin}{path}.js"
        payload = self._load_json(js_url)
        if isinstance(payload, dict):
            fill_from_shopify_payload(book, payload, origin, cents=True)
            book.refresh_text_fields()
            book.mark_origin_fields()

    def _shopify_collection_product_urls(
        self,
        page_url: str,
        year: str,
        max_products: int,
        include_unknown_year: bool,
        on_urls: Callable[[list[str]], None] | None = None,
        on_listed: Callable[[Book], None] | None = None,
    ) -> list[str] | None:
        handle = shopify_collection_handle(page_url)
        if not handle:
            if is_ybook_host(page_url):
                handle = "newest-products"
            else:
                return None
        parsed = urlparse(page_url if "://" in page_url else "https://" + page_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        urls: list[str] = []
        seen: set[str] = set()
        want_year = str(year or "").strip()
        page = 1
        take = 250
        try:
            while len(urls) < max_products and page < 400:
                self._check_cancel()
                api_url = f"{origin}/collections/{quote(handle)}/products.json?page={page}&limit={take}"
                payload = self._load_json(api_url)
                self.report.listing_pages += 1
                if not isinstance(payload, dict):
                    return None if page == 1 and not urls else urls
                items = payload.get("products")
                if not isinstance(items, list):
                    return None if page == 1 and not urls else urls
                if page == 1:
                    self._emit(
                        "pages",
                        {
                            "site": site_display_name(page_url),
                            "url": page_url,
                            "current": 1,
                            "total": 0,
                            "unlimited": max_products >= 50_000,
                            "limit": 0,
                        },
                    )
                if not items:
                    break
                batch: list[str] = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    listed = Book(url="")
                    fill_from_shopify_payload(listed, item, origin, cents=False)
                    listed.refresh_text_fields()
                    if not listed.url or listed.url in seen:
                        continue
                    if want_year and listed.year and not listed.matches_year(want_year, include_unknown_year):
                        continue
                    if want_year and not listed.year and not include_unknown_year:
                        continue
                    seen.add(listed.url)
                    urls.append(listed.url)
                    batch.append(listed.url)
                    if on_listed and listed.title:
                        listed.append_scan_log("Found in the catalog listing.")
                        listed.refresh_scan_status()
                        on_listed(listed)
                    if len(urls) >= max_products:
                        break
                if on_urls and batch:
                    on_urls(batch)
                self.progress(
                    f"{site_display_name(page_url)} catalog: {len(urls)} book(s) for {want_year or 'any year'} "
                    f"after Shopify page {page}…"
                )
                if len(items) < take or len(urls) >= max_products:
                    break
                page += 1
        except CrawlCancelled:
            raise
        except (SiteError, requests.RequestException):
            return None if not urls else urls
        return urls or None

    def _book_and_html(self, product_url: str, remember: bool = True) -> tuple[Book | None, str, str]:
        from book_cache import get_page_book, remember_page_book, save_page_cache

        cached = get_page_book(product_url)
        if cached and cached.title:
            if is_evrit_host(product_url):
                self.apply_evrit_product_apis(cached, cached.url or product_url)
                remember_page_book(cached)
            elif "/products/" in urlparse(product_url).path.lower():
                self.apply_shopify_product_json(cached, cached.url or product_url)
                remember_page_book(cached)
            self.report.product_cached += 1
            return cached, "", cached.url
        html, resolved = self.fetch(product_url)
        book = extract_book_from_html(html, resolved)
        self.apply_evrit_product_apis(book, resolved or product_url)
        self.apply_shopify_product_json(book, resolved or product_url)
        self.report.product_fetched += 1
        if remember and book.title and not is_query_listing_url(resolved):
            remember_page_book(book)
            save_page_cache()
        return book, html, resolved

    def _book_from_url(self, product_url: str, remember: bool = True) -> Book | None:
        book, _html, _resolved = self._book_and_html(product_url, remember=remember)
        return book

    def _evrit_group_product_urls(
        self,
        page_url: str,
        year: str,
        max_products: int,
        include_unknown_year: bool,
        on_urls: Callable[[list[str]], None] | None = None,
        on_listed: Callable[[Book], None] | None = None,
    ) -> list[str] | None:
        group_id = evrit_group_id(page_url)
        if not group_id:
            return None
        parsed = urlparse(page_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        skip = 0
        take = 200
        urls: list[str] = []
        seen: set[str] = set()
        want_year = str(year or "").strip()
        try:
            while len(urls) < max_products and skip < 20000:
                self._check_cancel()
                api_url = f"{origin}/api/group/{group_id}/products?skip={skip}&take={take}"
                html, _final = self.fetch(api_url)
                self.report.listing_pages += 1
                try:
                    payload = json.loads(html)
                except json.JSONDecodeError:
                    return None if skip == 0 else urls
                items = payload.get("Items") if isinstance(payload, dict) else None
                if not isinstance(items, list):
                    return None if skip == 0 else urls
                if skip == 0 and isinstance(payload, dict):
                    total_items = json_item_count(payload)
                    if total_items:
                        total_pages = max(1, (total_items + take - 1) // take)
                        self._emit(
                            "pages",
                            {
                                "site": site_display_name(page_url),
                                "url": page_url,
                                "current": 1,
                                "total": total_pages,
                                "unlimited": max_products >= 50_000,
                                "limit": 0,
                            },
                        )
                if not items:
                    break
                batch: list[str] = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    product_id = item.get("ProductID")
                    if not product_id:
                        continue
                    item_year = str(item.get("PublishYear") or "").strip()
                    probe = Book(url=f"{origin}/product/{product_id}", year=item_year)
                    if want_year and not probe.matches_year(want_year, include_unknown_year):
                        continue
                    product_url = f"{origin}/product/{product_id}"
                    if product_url in seen:
                        continue
                    seen.add(product_url)
                    urls.append(product_url)
                    batch.append(product_url)
                    if on_listed:
                        listed = Book(url=product_url)
                        fill_from_evrit_payload(listed, item)
                        listed.refresh_text_fields()
                        if listed.title:
                            listed.append_scan_log("Found in the catalog listing.")
                            listed.refresh_scan_status()
                        on_listed(listed)
                    if len(urls) >= max_products:
                        break
                if on_urls and batch:
                    on_urls(batch)
                host = site_display_name(page_url)
                self.progress(
                    f"{host} catalog: {len(urls)} book(s) for {want_year or 'any year'} "
                    f"after reading {skip + len(items)} listings…"
                )
                if len(items) < take or len(urls) >= max_products:
                    break
                skip += len(items)
        except CrawlCancelled:
            raise
        except (SiteError, requests.RequestException):
            return None if not urls else urls
        return urls

    def crawl(
        self,
        start_url: str,
        year: str,
        max_listing_pages: int = 5,
        max_products: int = 150,
        include_unknown_year: bool = True,
        start_error: bool = True,
        on_book: Callable[[Book], None] | None = None,
    ) -> list[Book]:
        start_url = start_url.strip()
        if not start_url.startswith(("http://", "https://")):
            start_url = "https://" + start_url
        start_url = catalog_listing_url(start_url)
        self.last_site_error = ""
        results: list[Book] = []
        seen_products: set[str] = set()
        site_name = site_display_name(start_url)

        page_cap = listing_page_cap(max_listing_pages)
        known_total = 0

        def keep(book: Book) -> tuple[Book, bool]:
            canonical, is_new = absorb_book(results, book)
            if on_book:
                on_book(canonical)
            return canonical, is_new

        def note_pages(current: int, total: int) -> None:
            self._emit(
                "pages",
                {
                    "site": site_name,
                    "url": start_url,
                    "current": current,
                    "total": total,
                    "unlimited": int(max_listing_pages or 0) <= 0,
                    "limit": 0 if int(max_listing_pages or 0) <= 0 else page_cap,
                },
            )

        def checking(index: int, total: int, book: Book | None = None) -> None:
            payload: dict[str, Any] = {
                "index": index,
                "total": max(total, 1),
                "found": len(results),
                "site": site_name,
                "url": start_url,
            }
            if book is not None:
                payload["book"] = book
                payload["title"] = book.display_title()
                payload["author"] = book.author
                payload["publisher"] = book.publisher
            self._emit("check", payload)

        def take_listed(product_url: str, listing: Book | None = None) -> Book | None:
            from book_cache import get_page_book

            if product_url in seen_products:
                if listing:
                    canonical, _is_new = keep(listing)
                    return canonical
                return None
            seen_products.add(product_url)
            cached = get_page_book(product_url)
            incoming = listing or Book(url=product_url)
            if cached and cached.title:
                if year and not cached.matches_year(year, include_unknown_year):
                    self.report.skipped_year += 1
                    return None
                incoming.merge_missing(cached)
            elif year and incoming.year and not incoming.matches_year(year, include_unknown_year):
                self.report.skipped_year += 1
                return None
            elif not (incoming.title or "").strip() and year and not include_unknown_year:
                return None
            canonical, is_new = keep(incoming)
            if is_new and (canonical.title or "").strip():
                self.report.matched += 1
            titled = sum(1 for item in results if (item.title or "").strip())
            self.report.product_links = max(self.report.product_links, len(seen_products))
            checking(titled, 0, canonical)
            self.progress(
                f"Listing book {titled} — {canonical.display_title()}"
                + (f" · {canonical.author}" if canonical.author else "")
                + (f" · {canonical.publisher}" if canonical.publisher else "")
                + f" — {site_name}"
            )
            return canonical

        def keep_listed(book: Book) -> None:
            take_listed(book.url, book)

        try:
            html, final_url = self.fetch(start_url)
            self.report.listing_pages += 1
        except (SiteError, requests.RequestException) as exc:
            self.report.listing_failed += 1
            message = str(exc) if isinstance(exc, SiteError) else "Could not open the start URL."
            self.last_site_error = message
            if start_error:
                self.report.error = message
            self.progress(message)
            _safe_flush_scan_files()
            return results
        soup = parse_html(html)

        if is_product_page(soup, final_url):
            book = extract_book_from_html(html, final_url)
            self.apply_evrit_product_apis(book, final_url)
            self.apply_shopify_product_json(book, final_url)
            self.report.product_fetched += 1
            self.report.product_links = 1
            if book.title:
                from book_cache import remember_page_book, save_page_cache

                remember_page_book(book)
                save_page_cache()
                book.append_scan_log("Opened a product page and read the book.")
                book.refresh_scan_status()
                if book.matches_year(year, include_unknown_year):
                    keep(book)
                    self.report.matched += 1
                else:
                    self.report.skipped_year += 1
                    book.append_scan_log(f"Skipped: publication year {book.year or 'unknown'} is not {year}.")
            else:
                book.mark_scan_failed("Opened the product page but could not read a title.")
                keep(book)
                self.report.product_failed += 1
            self.progress(f"Opened a book page. {len(results)} match(es) for this year.")
            _safe_flush_scan_files()
            return results

        listing_pages = [final_url]
        product_urls: list[str] = []
        visited_listings: set[str] = set()
        used_api = False

        def on_api_urls(batch: list[str]) -> None:
            for url in batch:
                if url not in product_urls:
                    product_urls.append(url)
            self.report.product_links = len(product_urls)

        try:
            api_urls = self._evrit_group_product_urls(
                final_url,
                year,
                max_products,
                include_unknown_year,
                on_urls=on_api_urls,
                on_listed=keep_listed,
            )
            used_api = api_urls is not None
            if not used_api:
                api_urls = self._shopify_collection_product_urls(
                    final_url,
                    year,
                    max_products,
                    include_unknown_year,
                    on_urls=on_api_urls,
                    on_listed=keep_listed,
                )
                used_api = api_urls is not None
            if used_api:
                self.report.product_links = len(product_urls)
            if not used_api:
                for listing_url in listing_pages:
                    self._check_cancel()
                    if listing_url in visited_listings or len(visited_listings) >= page_cap:
                        continue
                    visited_listings.add(listing_url)
                    if listing_url != final_url:
                        try:
                            html, listing_url = self.fetch(listing_url)
                            self.report.listing_pages += 1
                            soup = parse_html(html)
                        except (SiteError, requests.RequestException):
                            self.report.listing_failed += 1
                            continue
                    discovered = last_listing_page(soup, listing_url)
                    if discovered > known_total:
                        known_total = discovered
                    page_n = len(visited_listings)
                    if known_total:
                        self.progress(f"Reading catalog page {page_n} of {known_total}…")
                    else:
                        self.progress(f"Reading catalog page {page_n}…")
                    note_pages(page_n, known_total)
                    listed_books = collect_listing_books(soup, listing_url)
                    if listed_books:
                        for listed in listed_books:
                            if not same_domain(start_url, listed.url) or is_asset_url(listed.url):
                                continue
                            if listed.url not in product_urls:
                                product_urls.append(listed.url)
                            take_listed(listed.url, listed)
                    else:
                        for product_url, listing_title in collect_product_entries(soup, listing_url):
                            if not same_domain(start_url, product_url) or is_asset_url(product_url):
                                continue
                            if product_url not in product_urls:
                                product_urls.append(product_url)
                            take_listed(product_url, Book(url=product_url, title=listing_title))
                    next_cap = min(page_cap, known_total) if known_total else page_cap
                    if len(visited_listings) < next_cap:
                        for page_url in collect_pagination_links(soup, listing_url, next_cap):
                            if same_domain(start_url, page_url) and page_url not in listing_pages:
                                listing_pages.append(page_url)
                    self.report.product_links = len(product_urls[:max_products])
                    self.progress(
                        f"Listed {self.report.product_links} book(s) from catalog pages. "
                        "Product pages are not opened during Search."
                    )
                    if len(product_urls) >= max_products:
                        break
        except CrawlCancelled:
            self.report.cancelled = True
            self.progress(f"Stopped. Keeping {len(results)} matching book(s) found so far.")
        _safe_flush_scan_files()
        return results

    def search_urls_for_query(self, site_url: str, query: str) -> list[str]:
        parsed = urlparse(site_url if "://" in site_url else "https://" + site_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        host = parsed.netloc.lower()
        encoded = quote(query)
        if "e-vrit" in host or "evrit" in host:
            return [f"{origin}/search?q={encoded}"]
        if "ybook" in host:
            return [f"{origin}/search?q={encoded}"]
        if "booknet" in host:
            return [f"{origin}/%D7%97%D7%99%D7%A4%D7%95%D7%A9?q={encoded}"]
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
                product_urls = [url for url, _title in collect_cover_picture_links(soup, page_url)]
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
                self.progress("Opened the full book page.")
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
        except (SiteError, requests.RequestException):
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
            except (SiteError, requests.RequestException):
                html = ""
        if html:
            soup = parse_html(html)
            listing = listing or is_search_results_page(soup, resolved)
            if listing:
                if depth == 0:
                    self.progress("The site listed several books. Opening the matching book page…")
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
            self.progress(f"Reused a saved {host} book page.")
            if fills_needed(candidate, book) >= len(book.missing_fields()) and fillable_count(candidate) >= 5:
                return candidate
            try:
                html, final_url = self.fetch(candidate.url)
            except (SiteError, requests.RequestException):
                return candidate
            soup = parse_html(html)
            seen.add(_url_key(candidate.url))
            richer = self._follow_book_cover_links(soup, final_url, book, seen, 0, candidate)
            if richer and not is_query_listing_url(richer.url):
                remember_page_book(richer)
                save_page_cache()
                return richer
            return candidate

        home_html = ""
        home_url = origin.rstrip("/") + "/"
        try:
            home_html, home_url = self.fetch(home_url)
            seen.add(_url_key(home_url))
        except SiteError:
            raise
        except requests.RequestException:
            home_html = ""

        def consider(product_url: str, remember: bool) -> Book | None:
            return self._consider_book_page(product_url, book, seen, remember=remember)

        if try_slugs:
            for product_url in self._slug_urls_for_book(site_url, book):
                found = consider(product_url, remember=False)
                if found:
                    self.progress("Opened the matching book page.")
                    return found
            if home_html:
                soup = parse_html(home_html)
                homepage_links = collect_detail_links(soup, home_url, book) or collect_matching_links(
                    soup, home_url, book
                )
                for product_url in homepage_links[:8]:
                    found = consider(product_url, remember=False)
                    if found:
                        self.progress("Opened the matching book page.")
                        return found
        queries = [value for value in (book.isbn, book.display_title(), f"{book.title} {book.author}") if value]
        for query in queries:
            for search_url in self.search_urls_for_query(site_url, query):
                key = _url_key(search_url)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    html, final_url = self.fetch(search_url)
                except (SiteError, requests.RequestException):
                    continue
                soup = parse_html(html)
                if is_search_results_page(soup, final_url):
                    self.progress("The site listed several books. Opening the matching book page…")
                    product_urls = collect_search_result_links(soup, final_url, book)
                    if not product_urls:
                        product_urls = [url for url, _title in collect_cover_picture_links(soup, final_url)]
                    if not product_urls:
                        product_urls = collect_product_links(soup, final_url)
                    for product_url in product_urls[:16]:
                        found = consider(product_url, remember=False)
                        if found:
                            self.progress("Opened the matching book page.")
                            return found
                    continue
                page_book = extract_book_from_html(html, final_url)
                matched = page_book if page_book.title and books_match(book, page_book) else None
                if matched:
                    remember_page_book(matched)
                    save_page_cache()
                    richer = self._follow_book_cover_links(soup, final_url, book, seen, 0, matched)
                    if richer:
                        self.progress("Opened the matching book page.")
                        return richer
                    if is_query_listing_url(matched.url):
                        continue
                    self.progress("Opened the matching book page.")
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
                        self.progress("Opened the matching book page.")
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
            except SiteError as exc:
                note = str(exc)
                self.progress(note)
                book.mark_publisher_lookup(publisher_url, note=note, error=True)
                return filled
            except requests.RequestException as exc:
                note = request_failure_message(exc, publisher_url)
                self.progress(note)
                book.mark_publisher_lookup(publisher_url, note=note, error=True)
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
            site_name = site_display_name(extra_url)
            self._emit("site", {"url": extra_url, "name": site_name, "index": 0, "total": 0, "phase": "fill"})
            extras = cached_books_for_host(host)
            if not extras:
                extras = [book for book in books if book.has_page_on(extra_url)]
            if extras:
                self.progress(
                    f"Filling missing fields from pages already read on {site_name} "
                    f"({len(extras):,} page(s)). Not listing the catalog again."
                )
            elif max_searches > 0:
                self.progress(f"No cached {site_name} pages. Searching for matching book pages…")
            added = merge_catalog(books, extras) if extras else 0
            filled += added
            self.report.enriched += added
            if max_searches <= 0:
                continue
            pending = [
                book
                for book in books
                if not book.has_page_on(extra_url) and book.missing_fields() and (book.title or "").strip()
            ]
            pending.sort(key=lambda book: book.display_title())
            searches = 0
            search_total = min(max_searches, len(pending))
            for book in pending:
                if searches >= max_searches:
                    break
                searches += 1
                self.progress(
                    f"Matching catalog pages {searches} of {search_total} on {site_name}: "
                    f"{book.display_title()}"
                    + (f" · {book.author}" if book.author else "")
                    + (f" · {book.publisher}" if book.publisher else "")
                )
                self._emit(
                    "fill",
                    {
                        "book": book,
                        "index": searches,
                        "total": search_total,
                        "found": len(books),
                        "site": site_name,
                        "url": extra_url,
                    },
                )
                try:
                    match = self.find_matching_product(extra_url, book)
                except CrawlCancelled:
                    raise
                except SiteError as exc:
                    self.progress(str(exc))
                    break
                except requests.RequestException:
                    continue
                if not match:
                    self._emit("book", {"book": book, "new": False, "found": len(books), "site": site_name, "url": extra_url})
                    continue
                added_fields = book.merge_missing(match)
                if added_fields:
                    filled += 1
                    self.report.enriched += 1
                    self.progress(f"Filled extra details from {host} for {book.display_title()}")
                else:
                    self.progress(f"Found {host} book page for {book.display_title()}")
                self._emit("book", {"book": book, "new": False, "found": len(books), "site": site_name, "url": extra_url})
        _safe_flush_scan_files()
        for book in books:
            if book.title:
                book.refresh_scan_status()
        return filled

    def enrich_from_publishers(self, books: list[Book]) -> int:
        from publisher_sites import resolve_publisher_site

        pending = [
            book
            for book in books
            if (book.title or "").strip()
            and (book.publisher or "").strip()
            and resolve_publisher_site(book.publisher)
            and not (book.extra.get("publisher_page") or "").strip()
            and book.missing_fields()
        ]
        filled = 0
        total = len(pending)
        for index, book in enumerate(pending, start=1):
            self._check_cancel()
            publisher_url = resolve_publisher_site(book.publisher) or ""
            host = urlparse(publisher_url).netloc
            site_name = site_display_name(publisher_url) if publisher_url else host
            self.progress(
                f"Filling publisher details {index} of {total} on {host}: {book.display_title()}"
            )
            self._emit("site", {"url": publisher_url, "name": site_name or host, "index": index, "total": total})
            self._emit(
                "fill",
                {
                    "book": book,
                    "index": index,
                    "total": total,
                    "found": len(books),
                    "site": site_name or host,
                    "url": publisher_url,
                },
            )
            added = self.enrich_one_book(book)
            self._emit(
                "book",
                {
                    "book": book,
                    "new": False,
                    "found": len(books),
                    "site": site_name or host,
                    "url": publisher_url,
                },
            )
            if added:
                filled += 1
        return filled

    def search_all_sites(
        self,
        urls: list[str],
        year: str,
        max_listing_pages: int = 5,
        include_unknown_year: bool = True,
        seed_books: list[Book] | None = None,
        seed_stamp: str = "",
    ) -> list[Book]:
        books: list[Book] = list(seed_books or [])
        started_with = len(books)
        fill_missing_entry_dates(books, seed_stamp)
        for book in books:
            book.refresh_text_fields()
        listing_urls = unique_catalog_urls(urls)
        requested = int(max_listing_pages or 0)
        page_limit = listing_page_cap(requested)
        max_products = 50_000 if requested <= 0 else max(4000, page_limit * 100)
        listed = 0
        notes: list[str] = []
        try:
            for index, url in enumerate(listing_urls, start=1):
                self._check_cancel()
                host = site_display_name(url)
                self.progress(f"Site {index} of {len(listing_urls)}: listing books from {host}…")
                self._emit("site", {"url": url, "name": host, "index": index, "total": len(listing_urls)})
                self.last_site_error = ""
                before = len(books)
                found = self.crawl(
                    start_url=url,
                    year=year,
                    max_listing_pages=0 if requested <= 0 else page_limit,
                    max_products=max_products,
                    include_unknown_year=include_unknown_year,
                    start_error=False,
                    on_book=lambda book, site=host, site_url=url: self._take_book(books, book, site, site_url),
                )
                added = len(books) - before
                titled = len([book for book in found if book.title])
                listed += 1 if titled else 0
                if self.last_site_error:
                    short = self.last_site_error.split(".")[0]
                    notes.append(f"{host}: could not open ({short})")
                else:
                    notes.append(f"{host}: {titled} listed, {added} new")
                self.progress(
                    f"{host}: {titled} book(s) this year. "
                    f"{added} new name(s). Combined list: {len(books)}."
                )
            self.report.site_notes = notes
            self.report.error = ""
            self.report.matched = len(books)
            self.report.new_names = max(0, len(books) - started_with)
            if not books:
                detail = "; ".join(notes) if notes else "no catalog URLs"
                self.report.error = f"Could not list books from the bookstore or catalog URLs. {detail}."
                self.progress(self.report.error)
                return books
            self.progress(
                f"Listed {len(books):,} unique book(s) from {listed} site(s). "
                + " · ".join(notes)
                + ". Filling extra details from pages already in the cache. "
                "Publisher websites are skipped during Search (use More on a book)."
            )
            self.enrich_books(books, listing_urls, max_searches=0)
            self.report.matched = len(books)
            self.report.new_names = max(0, len(books) - started_with)
            self.report.site_notes = notes
            return books
        finally:
            try:
                from book_cache import flush_page_cache

                flush_page_cache()
            except Exception:
                pass


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
