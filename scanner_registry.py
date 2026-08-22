"""Stable scanner IDs and approval / Excel / final state for each book."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from book_crawler import Book, identity_keys

REGISTRY_PATH = Path(__file__).resolve().parent / "cache" / "scanner_registry.json"

_registry: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty() -> dict[str, Any]:
    return {"fingerprints": {}, "records": {}}


def load_registry() -> dict[str, Any]:
    global _registry
    if _registry is not None:
        return _registry
    if not REGISTRY_PATH.exists():
        _registry = _empty()
        return _registry
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("fingerprints", {})
    data.setdefault("records", {})
    _registry = data
    return _registry


def save_registry() -> None:
    data = load_registry()
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def scanner_fingerprints(book: Book) -> list[str]:
    keys = list(identity_keys(book))
    upc = "".join(ch for ch in (book.upc or "") if ch.isdigit())
    if len(upc) >= 8:
        keys.append(f"upc:{upc}")
    url = (book.url or "").strip()
    if url:
        parsed = urlparse(url)
        host = parsed.netloc.lower().lstrip("www.")
        path = unquote(parsed.path).rstrip("/") or "/"
        keys.append(f"url:{host}{path.casefold()}")
    scanner_id = (book.scanner_id or "").strip()
    if scanner_id:
        keys.append(f"id:{scanner_id}")
    return [key for key in dict.fromkeys(keys) if key]


def _new_id(seed: str) -> str:
    data = load_registry()
    digest = hashlib.sha1((seed or "book").encode("utf-8")).hexdigest()
    for size in (10, 12, 16, 20):
        candidate = f"SISU-{digest[:size]}"
        if candidate not in data["records"]:
            return candidate
    return f"SISU-{digest}"


def _record(scanner_id: str) -> dict[str, Any]:
    data = load_registry()
    item = data["records"].get(scanner_id)
    if not isinstance(item, dict):
        item = {
            "scanner_id": scanner_id,
            "approved": False,
            "excel_passed": False,
            "final": False,
            "approved_at": "",
            "excel_passed_at": "",
            "final_at": "",
        }
        data["records"][scanner_id] = item
    item.setdefault("scanner_id", scanner_id)
    item.setdefault("approved", False)
    item.setdefault("excel_passed", False)
    item.setdefault("final", False)
    return item


def attach_book(book: Book) -> str:
    """Give this book a stable scanner ID. Approval and Final stay on the current list."""
    data = load_registry()
    fingerprints = scanner_fingerprints(book)
    found = ""
    if (book.scanner_id or "").strip() and book.scanner_id in data["records"]:
        found = book.scanner_id
    if not found:
        for key in fingerprints:
            mapped = str(data["fingerprints"].get(key) or "").strip()
            if mapped:
                found = mapped
                break
    if not found:
        found = _new_id("|".join(fingerprints) or book.display_title() or book.url)
    book.scanner_id = found
    for key in scanner_fingerprints(book):
        data["fingerprints"][key] = found
    record = _record(found)
    return found


def attach_books(books: list[Book]) -> None:
    for book in books:
        attach_book(book)
    save_registry()


def persist_book_state(book: Book) -> None:
    if not book.scanner_id:
        attach_book(book)
    record = _record(book.scanner_id)
    if book.approved and not record.get("approved"):
        record["approved_at"] = _now()
    if book.excel_passed and not record.get("excel_passed"):
        record["excel_passed_at"] = _now()
    if book.final and not record.get("final"):
        record["final_at"] = _now()
    record["approved"] = bool(book.approved)
    record["excel_passed"] = bool(book.excel_passed)
    record["final"] = bool(book.final)
    for key in scanner_fingerprints(book):
        load_registry()["fingerprints"][key] = book.scanner_id
    save_registry()


def mark_excel_ids(scanner_ids: list[str] | set[str]) -> None:
    changed = False
    for scanner_id in scanner_ids:
        sid = str(scanner_id or "").strip()
        if not sid:
            continue
        record = _record(sid)
        if not record.get("excel_passed"):
            record["excel_passed"] = True
            record["excel_passed_at"] = _now()
            changed = True
    if changed:
        save_registry()
