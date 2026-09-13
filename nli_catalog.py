"""Search the National Library of Israel catalog by year.

The public pages on nli.org.il are a JavaScript Primo app behind Cloudflare, so
HTML scraping finds no books. This module talks to the Open Library Search API
instead.

Identify this algorithm by these catalog URLs (any path or hash on these hosts):

    https://www.nli.org.il/
    https://www.nli.org.il/he/search
    https://merhav.nli.org.il/
    https://api.nli.org.il/

A typical user search (Hebrew books for a publication year) looks like:

    https://www.nli.org.il/he/search?projectName=NLI#&q=any,contains,ספרים
        &qInclude=facet_lang,exact,heb|,|facet_searchcreationdate,exact,[2026 TO 2026]
        &t=books
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from book_crawler import Book, apply_identifier, extract_year, format_person_name, is_nli_host

API_BASE = "https://api.nli.org.il/openlibrary/search"
SITE_URLS = (
    "https://www.nli.org.il/",
    "https://www.nli.org.il/he/search",
    "https://www.nli.org.il/he/search?materialType=books",
    "https://merhav.nli.org.il/",
    "https://api.nli.org.il/",
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


class NliCatalogError(Exception):
    """The NLI Search API refused the request or returned no usable catalog."""


def matches_site(url: str) -> bool:
    return is_nli_host(url) or "api.nli.org.il" in (url or "").casefold()


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
    if record_id:
        return f"https://www.nli.org.il/he/books/{record_id}"
    return ""


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
    """Return Hebrew books for this publication year from the NLI Search API."""
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
            raise NliCatalogError(f"Could not reach the National Library Search API: {exc}") from exc
        if response.status_code == 429:
            extra = (
                " The shared guest key is rate-limited. Get a free personal key at "
                "https://api2.nli.org.il/signup/ and paste it in Settings → Site URLs."
                if used_guest
                else " Wait and try again, or use another National Library API key in Settings → Site URLs."
            )
            raise NliCatalogError("The National Library Search API rate limit was exceeded." + extra)
        if response.status_code in {401, 403} and "html" in (response.headers.get("content-type") or "").casefold():
            raise NliCatalogError(
                "The National Library website blocked the request. SISU searches it through "
                f"{API_BASE} instead of the JavaScript catalog page."
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
            progress(f"National Library catalog page {page:,} · {len(books):,} book(s) so far")
        batch_kept = 0
        for item in rows:
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
