"""Disk cache for crawled books so the UI can reload without scraping again."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

from book_crawler import Book

CACHE_DIR = Path(__file__).resolve().parent / "cache"
LAST_PATH = CACHE_DIR / "last_results.json"
PAGE_CACHE_PATH = CACHE_DIR / "pages.json"


def normalize_url(url: str) -> str:
    value = unquote((url or "").strip())
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def cache_key(urls: list[str], year: str) -> str:
    normalized = [normalize_url(url) for url in urls if str(url).strip()]
    return json.dumps({"urls": normalized, "year": str(year or "")}, ensure_ascii=False, sort_keys=True)


def _results_path(urls: list[str], year: str) -> Path:
    digest = hashlib.sha1(cache_key(urls, year).encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"results_{digest}.json"


def _read_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data["books"] = [Book.from_dict(item) for item in data.get("books") or []]
    for book in data["books"]:
        remember_page_book(book)
    return data


def save_books(books: list[Book], urls: list[str], year: str, report: dict[str, Any] | None = None) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "year": year,
        "urls": [normalize_url(url) for url in urls],
        "key": cache_key(urls, year),
        "books": [asdict(book) for book in books],
        "report": report or {},
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    LAST_PATH.write_text(text, encoding="utf-8")
    hashed = _results_path(urls, year)
    hashed.write_text(text, encoding="utf-8")
    for book in books:
        remember_page_book(book)
    save_page_cache()
    return LAST_PATH


def load_last() -> dict[str, Any] | None:
    return _read_payload(LAST_PATH)


def load_results(urls: list[str], year: str) -> dict[str, Any] | None:
    data = _read_payload(_results_path(urls, year))
    if data:
        return data
    last = load_last()
    if cache_matches(last, urls, year):
        return last
    return None


def cache_matches(data: dict[str, Any] | None, urls: list[str], year: str) -> bool:
    if not data:
        return False
    if data.get("key") == cache_key(urls, year):
        return True
    saved = [normalize_url(url) for url in (data.get("urls") or [])]
    current = [normalize_url(url) for url in urls]
    return saved == current and str(data.get("year") or "") == str(year or "")


_page_books: dict[str, dict[str, Any]] | None = None


def _load_page_map() -> dict[str, dict[str, Any]]:
    global _page_books
    if _page_books is not None:
        return _page_books
    if not PAGE_CACHE_PATH.exists():
        _page_books = {}
        return _page_books
    try:
        _page_books = json.loads(PAGE_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _page_books = {}
    return _page_books


def save_page_cache() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PAGE_CACHE_PATH.write_text(json.dumps(_load_page_map(), ensure_ascii=False), encoding="utf-8")


def remember_page_book(book: Book) -> None:
    if not book.url:
        return
    _load_page_map()[normalize_url(book.url)] = asdict(book)


def get_page_book(url: str) -> Book | None:
    data = _load_page_map().get(normalize_url(url))
    if not data:
        return None
    return Book.from_dict(data)


def cached_books_for_host(host: str) -> list[Book]:
    host = (host or "").lower().lstrip("www.")
    found: list[Book] = []
    for url, data in _load_page_map().items():
        netloc = urlparse(url).netloc.lower().lstrip("www.")
        if netloc == host:
            found.append(Book.from_dict(data))
    return found
