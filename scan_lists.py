"""Named scan lists, working session, and stash — not the old search-cache checkbox."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from book_cache import LAST_PATH, remember_page_book, save_page_cache
from book_crawler import Book

APP_DIR = Path(__file__).resolve().parent
LISTS_DIR = APP_DIR / "lists"
WORKING_PATH = LISTS_DIR / "working.json"
STASH_PATH = LISTS_DIR / "stash.json"
INDEX_PATH = LISTS_DIR / "index.json"
NAMED_DIR = LISTS_DIR / "named"


def now_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_scan_title(book_count: int = 0, year: str = "") -> str:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    parts = [f"Scan {stamp}"]
    if year:
        parts.append(f"year {year}")
    if book_count:
        parts.append(f"{book_count} book{'s' if book_count != 1 else ''}")
    return " · ".join(parts)


def _ensure_dirs() -> None:
    NAMED_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    _ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def books_from_payload(data: dict[str, Any] | None) -> list[Book]:
    if not data:
        return []
    books = [Book.from_dict(item) for item in data.get("books") or []]
    for book in books:
        remember_page_book(book)
    if books:
        save_page_cache()
    return books


def empty_payload(
    *,
    title: str = "New",
    urls: list[str] | None = None,
    year: str = "",
    max_pages: int = 5,
    include_unknown: bool = True,
) -> dict[str, Any]:
    stamp = now_stamp()
    return {
        "id": "",
        "title": title or "New",
        "created_at": stamp,
        "updated_at": stamp,
        "locked": False,
        "archived": False,
        "year": year,
        "urls": list(urls or []),
        "max_pages": max_pages,
        "include_unknown": include_unknown,
        "books": [],
        "report": {},
        "notes": "",
    }


def build_payload(
    *,
    books: list[Book],
    urls: list[str],
    year: str,
    title: str,
    list_id: str = "",
    locked: bool = False,
    archived: bool = False,
    max_pages: int = 5,
    include_unknown: bool = True,
    report: dict[str, Any] | None = None,
    notes: str = "",
    created_at: str = "",
) -> dict[str, Any]:
    stamp = now_stamp()
    return {
        "id": list_id or "",
        "title": (title or "").strip() or default_scan_title(len(books), year),
        "created_at": created_at or stamp,
        "updated_at": stamp,
        "locked": bool(locked),
        "archived": bool(archived),
        "year": year,
        "urls": list(urls),
        "max_pages": int(max_pages or 5),
        "include_unknown": bool(include_unknown),
        "books": [asdict(book) for book in books],
        "report": report or {},
        "notes": notes or "",
    }


def _index() -> dict[str, Any]:
    data = _read_json(INDEX_PATH) or {}
    lists = data.get("lists")
    if not isinstance(lists, list):
        lists = []
    data["lists"] = lists
    return data


def _save_index(data: dict[str, Any]) -> None:
    _write_json(INDEX_PATH, data)


def _named_path(list_id: str) -> Path:
    return NAMED_DIR / f"{list_id}.json"


def _upsert_index(payload: dict[str, Any]) -> None:
    list_id = str(payload.get("id") or "").strip()
    if not list_id:
        return
    data = _index()
    item = {
        "id": list_id,
        "title": payload.get("title") or "Untitled",
        "created_at": payload.get("created_at") or "",
        "updated_at": payload.get("updated_at") or "",
        "locked": bool(payload.get("locked")),
        "archived": bool(payload.get("archived")),
        "year": payload.get("year") or "",
        "book_count": len(payload.get("books") or []),
    }
    lists = data["lists"]
    for index, existing in enumerate(lists):
        if str(existing.get("id") or "") == list_id:
            lists[index] = item
            break
    else:
        lists.append(item)
    lists.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    _save_index(data)


def list_summaries(*, include_archived: bool = False) -> list[dict[str, Any]]:
    rows = []
    for item in _index().get("lists") or []:
        if not include_archived and item.get("archived"):
            continue
        rows.append(item)
    return rows


def load_named(list_id: str) -> dict[str, Any] | None:
    return _read_json(_named_path(list_id))


def save_named(payload: dict[str, Any]) -> dict[str, Any]:
    list_id = str(payload.get("id") or "").strip() or uuid.uuid4().hex[:12]
    payload = dict(payload)
    payload["id"] = list_id
    payload["updated_at"] = now_stamp()
    if not payload.get("created_at"):
        payload["created_at"] = payload["updated_at"]
    _write_json(_named_path(list_id), payload)
    _upsert_index(payload)
    return payload


def delete_named(list_id: str) -> None:
    path = _named_path(list_id)
    if path.exists():
        path.unlink()
    data = _index()
    data["lists"] = [item for item in data["lists"] if str(item.get("id") or "") != list_id]
    _save_index(data)


def rename_named(list_id: str, title: str) -> dict[str, Any] | None:
    payload = load_named(list_id)
    if not payload:
        return None
    payload["title"] = title.strip() or payload.get("title") or "Untitled"
    return save_named(payload)


def set_named_flags(list_id: str, *, locked: bool | None = None, archived: bool | None = None) -> dict[str, Any] | None:
    payload = load_named(list_id)
    if not payload:
        return None
    if locked is not None:
        payload["locked"] = bool(locked)
    if archived is not None:
        payload["archived"] = bool(archived)
    return save_named(payload)


def save_working(payload: dict[str, Any]) -> Path:
    payload = dict(payload)
    payload["updated_at"] = now_stamp()
    if not payload.get("created_at"):
        payload["created_at"] = payload["updated_at"]
    for book in payload.get("books") or []:
        if isinstance(book, dict):
            remember_page_book(Book.from_dict(book))
    save_page_cache()
    return _write_json(WORKING_PATH, payload)


def load_working() -> dict[str, Any] | None:
    data = _read_json(WORKING_PATH)
    if data:
        return data
    legacy = _read_json(LAST_PATH)
    if not legacy:
        return None
    migrated = empty_payload(
        title=default_scan_title(len(legacy.get("books") or []), str(legacy.get("year") or "")),
        urls=list(legacy.get("urls") or []),
        year=str(legacy.get("year") or ""),
    )
    migrated["books"] = legacy.get("books") or []
    migrated["report"] = legacy.get("report") or {}
    save_working(migrated)
    return migrated


def stash_exists() -> bool:
    return STASH_PATH.exists()


def save_stash(payload: dict[str, Any]) -> Path:
    payload = dict(payload)
    payload["updated_at"] = now_stamp()
    return _write_json(STASH_PATH, payload)


def load_stash() -> dict[str, Any] | None:
    return _read_json(STASH_PATH)


def stash_summary() -> str:
    data = load_stash()
    if not data:
        return ""
    title = data.get("title") or "Stash"
    count = len(data.get("books") or [])
    when = str(data.get("updated_at") or "")[:16].replace("T", " ")
    return f"{title} · {count} book(s)" + (f" · {when}" if when else "")
