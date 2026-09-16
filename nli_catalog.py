"""Search the National Library of Israel catalog by year.

The public pages on www.nli.org.il and merhav.nli.org.il are a JavaScript app
behind Cloudflare, so HTML scraping finds no books. The documented Open Library
Search API at api.nli.org.il needs a personal key; the published guest key is
shared and returns HTTP 429 OVER_RATE_LIMIT.

What works from this app, with no key, is NLI's public Alma SRU target (the same
catalog librarians publish for Z39.50):

    https://nli.alma.exlibrisgroup.com/view/sru/972NNL_INST

Hebrew books for a publication year (for example 2026):

    query=alma.language=heb AND alma.mms_material_type=BK AND alma.main_pub_date=2026
    recordSchema=marcxml
    maximumRecords=50
    startRecord=1, 51, 101, ...

Identify this algorithm by these catalog URLs (any path or hash on these hosts):

    https://www.nli.org.il/
    https://www.nli.org.il/he/search
    https://merhav.nli.org.il/
    https://api.nli.org.il/
    https://nli.alma.exlibrisgroup.com/

A typical user search on the website looks like:

    https://www.nli.org.il/he/search?projectName=NLI#&q=any,contains,ספרים
        &qInclude=facet_lang,exact,heb|,|facet_searchcreationdate,exact,[2026 TO 2026]
        &t=books

If a personal Open Library API key is set in Settings, that API is used only
when SRU cannot be reached.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from book_crawler import Book, apply_identifier, extract_year, format_person_name, is_nli_host

SRU_BASE = "https://nli.alma.exlibrisgroup.com/view/sru/972NNL_INST"
SRU_PAGE_SIZE = 50
API_BASE = "https://api.nli.org.il/openlibrary/search"
SITE_URLS = (
    "https://www.nli.org.il/",
    "https://www.nli.org.il/he/search",
    "https://www.nli.org.il/he/search?materialType=books",
    "https://merhav.nli.org.il/",
    "https://api.nli.org.il/",
    "https://nli.alma.exlibrisgroup.com/",
)
DEFAULT_QUERY = "any,contains,ספר"
DEFAULT_LANGUAGE = "heb"
GUEST_API_KEY = "DVQyidFLOAjp12ib92pNJPmflmB5IessOq1CJQDK"
PAGE_SIZE = 50
DC = "http://purl.org/dc/elements/1.1/"
DC_TERMS = "http://purl.org/dc/terms/"
_GENERIC_BOOK_QUERY = re.compile(
    r"^(any|title),(contains|exact),(ספרים|ספר|books?)$",
    re.I,
)
_HEADING_NOISE = re.compile(r"\s*PreferredLanguageHeading\s*$", re.I)
_PAGES_RE = re.compile(r"(\d+)\s*(?:עמודים|עמוד|עמ['׳.]|pages?|p\.)", re.I)


class NliCatalogError(Exception):
    """The National Library catalog refused the request or returned no usable records."""


def matches_site(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").casefold()
    return (
        is_nli_host(url)
        or "api.nli.org.il" in host
        or host.endswith("alma.exlibrisgroup.com")
    )


def resolve_api_key(configured: str = "") -> str:
    return (
        (configured or "").strip()
        or str(os.environ.get("NLI_API_KEY") or "").strip()
        or GUEST_API_KEY
    )


def parse_catalog_url(url: str) -> dict[str, str]:
    """Read query / hash facets from an NLI search URL the user pasted."""
    parsed = urlparse(url or "")
    blob = "&".join(part for part in (parsed.query, unquote(parsed.fragment.lstrip("#&"))) if part)
    qs = parse_qs(blob, keep_blank_values=True)
    out = {"query": "", "language": "", "year": "", "material": ""}
    raw_q = (qs.get("q") or [""])[0].strip()
    if raw_q and re.match(r"^[a-z_]+,(?:contains|exact|begins_with),", raw_q, re.I):
        out["query"] = raw_q
    include = (qs.get("qInclude") or qs.get("qinclude") or [""])[0]
    for facet in include.split("|,|"):
        parts = [part.strip() for part in facet.split(",")]
        if len(parts) < 3:
            continue
        field, _op, value = parts[0], parts[1], ",".join(parts[2:])
        if field in {"facet_lang", "lang"} and value:
            out["language"] = value.split("|")[0].strip()
        if "creationdate" in field.casefold() or field.casefold() in {"facet_searchcreationdate", "cdate"}:
            match = re.search(r"(19|20)\d{2}", value)
            if match:
                out["year"] = match.group(0)
    tab = (qs.get("t") or qs.get("tab") or [""])[0].strip().casefold()
    if tab in {"books", "book"}:
        out["material"] = "books"
    if (qs.get("materialType") or qs.get("material_type") or [""])[0].strip().casefold() == "books":
        out["material"] = "books"
    return out


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_heading(value: str) -> str:
    return _HEADING_NOISE.sub("", _text(value)).strip(" ,;/")


def _marc_sub(field: ET.Element, code: str) -> str:
    bits: list[str] = []
    for sub in field:
        if _local_tag(sub.tag) == "subfield" and sub.get("code") == code:
            text = _text(sub.text)
            if text:
                bits.append(text)
    return " ".join(bits)


def _marc_fields(record: ET.Element, tag: str) -> list[ET.Element]:
    found: list[ET.Element] = []
    for child in record:
        name = _local_tag(child.tag)
        if name == "datafield" and child.get("tag") == tag:
            found.append(child)
        elif name == "controlfield" and child.get("tag") == tag:
            found.append(child)
    return found


def _marc_control(record: ET.Element, tag: str) -> str:
    for field in _marc_fields(record, tag):
        text = _text(field.text)
        if text:
            return text
    return ""


def record_url(mms_id: str) -> str:
    ident = re.sub(r"\s+", "", mms_id or "")
    if not ident:
        return ""
    return f"https://www.nli.org.il/he/books/{ident}"


def _title_from_marc(record: ET.Element) -> str:
    for field in _marc_fields(record, "245"):
        title = " ".join(part for part in (_marc_sub(field, "a"), _marc_sub(field, "b")) if part)
        title = title.strip(" /:;,")
        if title:
            return title
    return ""


def _author_from_marc(record: ET.Element) -> str:
    for tag in ("100", "110", "700"):
        for field in _marc_fields(record, tag):
            name = _clean_heading(_marc_sub(field, "a"))
            if name:
                return format_person_name(name) or name
    return ""


def _publisher_from_marc(record: ET.Element) -> str:
    for tag in ("264", "260"):
        for field in _marc_fields(record, tag):
            pub = _clean_heading(_marc_sub(field, "b")).strip("[],")
            if pub:
                return pub
    return ""


def _pages_from_marc(record: ET.Element) -> str:
    for field in _marc_fields(record, "300"):
        blob = " ".join(_text(sub.text) for sub in field if _local_tag(sub.tag) == "subfield")
        match = _PAGES_RE.search(blob)
        if match:
            return match.group(1)
    return ""


def _year_from_marc(record: ET.Element, fallback: str = "") -> str:
    for tag in ("264", "260"):
        for field in _marc_fields(record, tag):
            year = extract_year(_marc_sub(field, "c"))
            if year:
                return year
    return extract_year(fallback) or str(fallback or "").strip()


def book_from_marc(record: ET.Element, *, year: str = "") -> Book | None:
    if record is None:
        return None
    title = _title_from_marc(record)
    mms = _marc_control(record, "001")
    url = record_url(mms)
    if not title or not url:
        return None
    book = Book(url=url, title=title)
    book.author = _author_from_marc(record)
    book.publisher = _publisher_from_marc(record)
    book.year = _year_from_marc(record, year)
    book.pages = _pages_from_marc(record)
    book.language = "Hebrew"
    book.item_type = "book"
    if mms:
        digits = re.sub(r"\D", "", mms)
        if len(digits) >= 6:
            book.marc = digits
    for field in _marc_fields(record, "020"):
        apply_identifier(book, _marc_sub(field, "a"))
    book.append_scan_log("Found in the National Library catalog.")
    book.refresh_text_fields()
    book.refresh_scan_status()
    return book


def _is_marc_record(el: ET.Element) -> bool:
    if _local_tag(el.tag) != "record":
        return False
    return any(_local_tag(child.tag) in {"leader", "controlfield", "datafield"} for child in el)


def _marc_records(root: ET.Element) -> list[ET.Element]:
    return [el for el in root.iter() if _is_marc_record(el)]


def build_sru_query(catalog_url: str, year: str) -> str:
    parsed = parse_catalog_url(catalog_url)
    clauses = [f"alma.language={parsed['language'] or DEFAULT_LANGUAGE}"]
    if (parsed["material"] or "books") in {"books", "book", ""}:
        clauses.append("alma.mms_material_type=BK")
    want_year = str(year or parsed["year"] or "").strip()
    if want_year:
        clauses.append(f"alma.main_pub_date={want_year}")
    raw_query = parsed["query"]
    if raw_query and not _is_generic_book_query(raw_query):
        parts = [part.strip() for part in raw_query.split(",", 2)]
        if len(parts) == 3 and parts[0] in {"title", "creator", "publisher"}:
            field = {"title": "title", "creator": "creator", "publisher": "publisher"}[parts[0]]
            clauses.append(f"alma.{field}={parts[2]}")
    return " AND ".join(clauses)


def _sru_total(root: ET.Element) -> int:
    for el in root.iter():
        if _local_tag(el.tag) == "numberOfRecords":
            try:
                return max(0, int(_text(el.text) or 0))
            except ValueError:
                return 0
    return 0


def _sru_diagnostic(root: ET.Element) -> str:
    messages: list[str] = []
    for el in root.iter():
        if _local_tag(el.tag) in {"message", "details"}:
            text = _text(el.text)
            if text:
                messages.append(text)
    return "; ".join(messages[:3])


def _fetch_sru_page(session, query: str, start: int) -> tuple[int, list[ET.Element]]:
    params = {
        "version": "1.2",
        "operation": "searchRetrieve",
        "recordSchema": "marcxml",
        "maximumRecords": str(SRU_PAGE_SIZE),
        "startRecord": str(max(1, start)),
        "query": query,
    }
    url = f"{SRU_BASE}?{urlencode(params)}"
    try:
        response = session.get(url, timeout=45)
    except Exception as exc:
        raise NliCatalogError(f"Could not reach the National Library catalog: {exc}") from exc
    if response.status_code >= 400:
        raise NliCatalogError(f"National Library catalog HTTP {response.status_code}.")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        snippet = (response.text or "")[:120].casefold()
        if "checking your browser" in snippet or "just a moment" in snippet:
            raise NliCatalogError(
                "The National Library website blocked the request. SISU reads the Alma catalog instead."
            ) from exc
        raise NliCatalogError("The National Library catalog did not return XML.") from exc
    diagnostic = _sru_diagnostic(root)
    records = _marc_records(root)
    total = _sru_total(root)
    if not records and diagnostic and total == 0 and "records" not in diagnostic.casefold():
        raise NliCatalogError(f"National Library catalog: {diagnostic}")
    return total, records


def search_sru_year_books(
    session,
    *,
    year: str,
    catalog_url: str,
    max_books: int = 10_000,
    include_unknown_year: bool = True,
    page_limit: int = 400,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    on_listed: Callable[[Book], None] | None = None,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Book]:
    query = build_sru_query(catalog_url, year)
    want_year = str(year or parse_catalog_url(catalog_url).get("year") or "").strip()
    books: list[Book] = []
    seen: set[str] = set()
    cap = max(1, int(max_books or 1))
    pages = max(1, int(page_limit or 1))
    start = 1
    total = 0
    for page in range(1, pages + 1):
        if cancelled and cancelled():
            break
        if progress:
            extra = f" of {total:,}" if total else ""
            progress(f"National Library catalog page {page:,}{extra} · {len(books):,} book(s) so far")
        try:
            page_total, records = _fetch_sru_page(session, query, start)
        except NliCatalogError:
            if cancelled and cancelled():
                break
            raise
        if cancelled and cancelled():
            break
        if page_total:
            total = page_total
        if on_page:
            on_page(page, min(pages, max(1, (total + SRU_PAGE_SIZE - 1) // SRU_PAGE_SIZE) if total else pages))
        if not records:
            break
        batch_kept = 0
        for record in records:
            if cancelled and cancelled():
                return books
            book = book_from_marc(record, year=want_year)
            if not book:
                continue
            if want_year and not book.matches_year(want_year, include_unknown_year):
                continue
            if book.url in seen:
                continue
            seen.add(book.url)
            books.append(book)
            batch_kept += 1
            if on_listed:
                on_listed(book)
            if len(books) >= cap:
                return books
        start += max(len(records), 1)
        if total and start > total:
            break
        if len(records) < SRU_PAGE_SIZE:
            break
        if batch_kept == 0 and page >= 3:
            break
        if cancelled and cancelled():
            break
        deadline = time.monotonic() + 0.12
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                return books
            time.sleep(min(0.05, deadline - time.monotonic()))
    return books


def _dc_values(item: dict[str, Any], name: str) -> list[str]:
    raw = item.get(f"{DC}{name}") or item.get(f"{DC_TERMS}{name}") or item.get(name) or []
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    values: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            text = str(entry.get("@value") or entry.get("@id") or "").strip()
        else:
            text = str(entry or "").strip()
        if text:
            values.append(text)
    return values


def _dc_text(item: dict[str, Any], name: str) -> str:
    values = _dc_values(item, name)
    return values[0] if values else ""


def _record_url(item: dict[str, Any], record_id: str) -> str:
    link = str(item.get("@id") or "").strip()
    if link.startswith("http"):
        return link
    return record_url(record_id)


def book_from_record(item: dict[str, Any]) -> Book | None:
    if not isinstance(item, dict):
        return None
    title = _dc_text(item, "title")
    record_id = _dc_text(item, "recordid") or _dc_text(item, "identifier")
    url = _record_url(item, record_id)
    if not title or not url:
        return None
    book = Book(url=url, title=title)
    creators = _dc_values(item, "creator")
    if creators:
        book.author = format_person_name(creators[0]) or creators[0]
    publishers = _dc_values(item, "publisher")
    if publishers:
        book.publisher = publishers[0]
    book.year = extract_year(_dc_text(item, "date") or _dc_text(item, "created"))
    if record_id:
        digits = re.sub(r"\D", "", record_id)
        if len(digits) >= 6:
            book.marc = digits
    for ident in _dc_values(item, "identifier"):
        apply_identifier(book, ident)
    book.append_scan_log("Found in the National Library catalog.")
    book.refresh_text_fields()
    book.refresh_scan_status()
    return book


def _is_generic_book_query(query: str) -> bool:
    """Primo 'ספרים' / 'books' keywords are not a real title filter."""
    return bool(_GENERIC_BOOK_QUERY.match((query or "").strip()))


def build_query(catalog_url: str, year: str) -> tuple[str, str]:
    parsed = parse_catalog_url(catalog_url)
    clauses: list[str] = []
    raw_query = parsed["query"]
    if raw_query and not _is_generic_book_query(raw_query):
        clauses.append(raw_query)
    language = parsed["language"] or DEFAULT_LANGUAGE
    if language:
        clauses.append(f"language,exact,{language}")
    want_year = str(year or parsed["year"] or "").strip()
    if want_year:
        clauses.append(f"start_date,contains,{want_year}")
    if not clauses:
        clauses.append(DEFAULT_QUERY)
    material = parsed["material"] or "books"
    return ",AND;".join(clauses), material


def search_openlibrary_year_books(
    session,
    *,
    year: str,
    catalog_url: str,
    api_key: str = "",
    max_books: int = 10_000,
    include_unknown_year: bool = True,
    page_limit: int = 40,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    on_listed: Callable[[Book], None] | None = None,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Book]:
    """Hebrew books for this year from the Open Library Search API (needs a personal key)."""
    key = resolve_api_key(api_key)
    query, material = build_query(catalog_url, year)
    want_year = str(year or parse_catalog_url(catalog_url).get("year") or "").strip()
    books: list[Book] = []
    seen: set[str] = set()
    pages = max(1, int(page_limit or 1))
    cap = max(1, int(max_books or 1))
    used_guest = key == GUEST_API_KEY

    for page in range(1, pages + 1):
        if cancelled and cancelled():
            break
        params = {
            "api_key": key,
            "query": query,
            "material_type": material,
            "output_format": "json",
            "items_per_page": str(PAGE_SIZE),
            "result_page": str(page),
            "sort_field": "date_desc",
        }
        url = f"{API_BASE}?{urlencode(params, safe=',;')}"
        try:
            response = session.get(url, timeout=30)
        except Exception as exc:
            if cancelled and cancelled():
                break
            raise NliCatalogError(f"Could not reach the National Library Search API: {exc}") from exc
        if cancelled and cancelled():
            break
        if response.status_code == 429:
            extra = (
                " The shared guest key is rate-limited. SISU now reads the Alma catalog instead."
                if used_guest
                else " Wait and try again, or use another National Library API key in Settings → Site URLs."
            )
            raise NliCatalogError("The National Library Search API rate limit was exceeded." + extra)
        if response.status_code in {401, 403} and "html" in (response.headers.get("content-type") or "").casefold():
            raise NliCatalogError(
                "The National Library website blocked the request. SISU searches the Alma catalog instead."
            )
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = str((payload.get("error") or {}).get("message") or payload.get("code") or "")
            except Exception:
                detail = (response.text or "")[:180]
            if response.status_code in {401, 403} or "API_KEY" in (detail or "").upper():
                raise NliCatalogError(
                    "The National Library API key was rejected. Get a free key at "
                    "https://api2.nli.org.il/signup/ and paste it in Settings → Site URLs."
                )
            raise NliCatalogError(f"National Library Search API HTTP {response.status_code}: {detail or 'request failed'}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            snippet = (response.text or "")[:120].casefold()
            if "checking your browser" in snippet or "just a moment" in snippet:
                raise NliCatalogError(
                    "The National Library returned a browser-check page instead of the catalog API."
                ) from exc
            raise NliCatalogError("The National Library Search API did not return JSON.") from exc
        rows = payload if isinstance(payload, list) else payload.get("records") or payload.get("items") or []
        if not isinstance(rows, list) or not rows:
            break
        if on_page:
            on_page(page, pages)
        if progress:
            progress(f"National Library API page {page:,} · {len(books):,} book(s) so far")
        batch_kept = 0
        for item in rows:
            if cancelled and cancelled():
                return books
            book = book_from_record(item)
            if not book:
                continue
            if want_year and not book.matches_year(want_year, include_unknown_year):
                continue
            if book.url in seen:
                continue
            seen.add(book.url)
            books.append(book)
            batch_kept += 1
            if on_listed:
                on_listed(book)
            if len(books) >= cap:
                return books
        if len(rows) < PAGE_SIZE:
            break
        if batch_kept == 0 and page >= 3:
            break
    return books


def search_year_books(
    session,
    *,
    year: str,
    catalog_url: str,
    api_key: str = "",
    max_books: int = 10_000,
    include_unknown_year: bool = True,
    page_limit: int = 40,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    on_listed: Callable[[Book], None] | None = None,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Book]:
    """Return Hebrew books for this publication year from the National Library catalog."""
    try:
        return search_sru_year_books(
            session,
            year=year,
            catalog_url=catalog_url,
            max_books=max_books,
            include_unknown_year=include_unknown_year,
            page_limit=page_limit,
            cancelled=cancelled,
            progress=progress,
            on_listed=on_listed,
            on_page=on_page,
        )
    except NliCatalogError as sru_exc:
        key = resolve_api_key(api_key)
        if not key or key == GUEST_API_KEY:
            raise
        if progress:
            progress("Alma catalog failed; trying the Open Library API key.")
        try:
            return search_openlibrary_year_books(
                session,
                year=year,
                catalog_url=catalog_url,
                api_key=key,
                max_books=max_books,
                include_unknown_year=include_unknown_year,
                page_limit=min(int(page_limit or 1), 40),
                cancelled=cancelled,
                progress=progress,
                on_listed=on_listed,
                on_page=on_page,
            )
        except NliCatalogError as api_exc:
            raise NliCatalogError(f"{sru_exc} {api_exc}") from api_exc


def _cli(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    year = "2026"
    limit = 8
    if args:
        year = args[0]
    if len(args) > 1:
        try:
            limit = max(1, int(args[1]))
        except ValueError:
            limit = 8
    import requests

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/xml,text/xml,*/*",
            "Accept-Language": "he,en;q=0.8",
        }
    )
    books = search_year_books(
        session,
        year=year,
        catalog_url="https://www.nli.org.il/he/search?materialType=books",
        max_books=limit,
        page_limit=3,
        progress=lambda msg: print(msg, file=sys.stderr),
    )
    print(f"{len(books)} book(s) for {year}")
    for book in books:
        bits = [book.year or "?", book.title]
        if book.author:
            bits.append(book.author)
        if book.publisher:
            bits.append(book.publisher)
        if book.isbn:
            bits.append(book.isbn)
        print(" · ".join(bits))
    return 0 if books else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
