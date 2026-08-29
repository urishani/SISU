"""Disk cache for crawled books so the UI can reload without scraping again."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

from book_crawler import Book

CACHE_DIR = Path(__file__).resolve().parent / "cache"
LAST_PATH = CACHE_DIR / "last_results.json"
PAGE_CACHE_PATH = CACHE_DIR / "pages.json"
_PAGE_CACHE_SAVE_INTERVAL = 8.0
_page_lock = threading.Lock()
_page_books: dict[str, dict[str, Any]] | None = None
_page_cache_dirty = False
_page_cache_unreadable = False
_last_page_save = 0.0


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


def _make_writable(path: Path) -> None:
    try:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
        except Exception:
            pass


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(12):
            try:
                if path.exists():
                    _make_writable(path)
                os.replace(tmp_name, path)
                return
            except OSError:
                time.sleep(min(0.08 * (2 ** attempt), 1.25))
        try:
            if path.exists():
                _make_writable(path)
            path.write_text(text, encoding="utf-8")
            return
        except OSError:
            return
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass


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
    try:
        _write_text_atomic(LAST_PATH, text)
        _write_text_atomic(_results_path(urls, year), text)
    except OSError:
        pass
    for book in books:
        remember_page_book(book)
    flush_page_cache()
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


def _load_page_map() -> dict[str, dict[str, Any]]:
    global _page_books, _page_cache_unreadable
    if _page_books is not None:
        return _page_books
    if not PAGE_CACHE_PATH.exists():
        _page_books = {}
        return _page_books
    try:
        loaded = json.loads(PAGE_CACHE_PATH.read_text(encoding="utf-8"))
        _page_books = loaded if isinstance(loaded, dict) else {}
        _page_cache_unreadable = False
    except PermissionError:
        _page_books = {}
        _page_cache_unreadable = True
    except (OSError, json.JSONDecodeError):
        _page_books = {}
        _page_cache_unreadable = False
    return _page_books


def save_page_cache(*, force: bool = False) -> None:
    global _page_cache_dirty, _last_page_save
    with _page_lock:
        pages = _load_page_map()
        if _page_cache_unreadable:
            return
        if not force and not _page_cache_dirty:
            return
        now = time.monotonic()
        if not force and (now - _last_page_save) < _PAGE_CACHE_SAVE_INTERVAL:
            return
        snapshot = dict(pages)
    try:
        _write_text_atomic(PAGE_CACHE_PATH, json.dumps(snapshot, ensure_ascii=False))
    except Exception:
        return
    with _page_lock:
        _last_page_save = time.monotonic()
        if len(_load_page_map()) <= len(snapshot):
            _page_cache_dirty = False


def flush_page_cache() -> None:
    try:
        save_page_cache(force=True)
    except Exception:
        pass


def remember_page_book(book: Book) -> None:
    global _page_cache_dirty
    if not book.url:
        return
    with _page_lock:
        _load_page_map()[normalize_url(book.url)] = asdict(book)
        _page_cache_dirty = True


def get_page_book(url: str) -> Book | None:
    with _page_lock:
        data = _load_page_map().get(normalize_url(url))
    if not data:
        return None
    return Book.from_dict(data)


def cached_books_for_host(host: str) -> list[Book]:
    host = (host or "").lower().lstrip("www.")
    with _page_lock:
        items = list(_load_page_map().items())
    found: list[Book] = []
    for url, data in items:
        netloc = urlparse(url).netloc.lower().lstrip("www.")
        if netloc == host:
            found.append(Book.from_dict(data))
    return found
