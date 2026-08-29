"""Timestamped execution logs. Progress lines with a slot are rewritten in place."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_DIR = Path(__file__).resolve().parent / "cache" / "logs"
MAX_RUNS = 40
_SAVE_INTERVAL = 2.0

_SLOT_PATTERNS = (
    ("check", re.compile(r"listing book\s+\d+|checking book\s+\d+", re.I)),
    ("pages", re.compile(r"catalog page\s+\d+|catalog:\s+[\d,]+\s+book", re.I)),
    ("discover", re.compile(r"discovered\s+[\d,]+\s+book|listed\s+[\d,]+\s+book\(s\) from catalog pages", re.I)),
    ("fill", re.compile(r"matching catalog pages\s+\d+\s+of\s+\d+|filling publisher details\s+\d+\s+of\s+\d+", re.I)),
    ("fill-site", re.compile(r"filling missing fields from", re.I)),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp_label(value: str) -> str:
    text = str(value or "").strip()
    if "T" in text:
        date, rest = text.split("T", 1)
        return f"{date} {rest.replace('Z', '')[:8]}"
    return text[:19]


def slot_for(text: str) -> str | None:
    for name, pattern in _SLOT_PATTERNS:
        if pattern.search(text or ""):
            return name
    return None


class ActivityLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: dict[str, Any] | None = None
        self._dirty = False
        self._last_save = 0.0
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def start_run(self, title: str, detail: str = "") -> str:
        stamp = _now()
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run = {
            "id": run_id,
            "started_at": stamp,
            "finished_at": "",
            "title": title or "Search",
            "detail": detail,
            "status": "running",
            "lines": [],
            "slots": {},
        }
        with self._lock:
            if self.current and self.current.get("status") == "running":
                self.current["status"] = "stopped"
                self.current["finished_at"] = stamp
                self._write_run(self.current)
            self.current = run
            self._dirty = True
        self.log(detail or f"{title} started.", slot=None)
        self.save(force=True)
        self._prune()
        return run_id

    def log(self, text: str, *, slot: str | None = "") -> None:
        message = str(text or "").strip()
        if not message:
            return
        if slot == "":
            slot = slot_for(message)
        entry = {"ts": _now(), "text": message, "slot": slot or ""}
        with self._lock:
            run = self.current
            if run is None:
                return
            lines: list[dict[str, Any]] = run["lines"]
            slots: dict[str, int] = run["slots"]
            if slot and slot in slots and 0 <= slots[slot] < len(lines):
                lines[slots[slot]] = entry
            else:
                if slot:
                    slots[slot] = len(lines)
                lines.append(entry)
            self._dirty = True
        self.save()

    def finish(self, status: str, summary: str = "") -> None:
        if summary:
            self.log(summary)
        with self._lock:
            run = self.current
            if run is None:
                return
            run["status"] = status
            run["finished_at"] = _now()
            self._dirty = True
            self._write_run(run)
            self._dirty = False
            self._last_save = time.monotonic()

    def save(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            run = self.current
            if run is None or not self._dirty:
                return
            if not force and (now - self._last_save) < _SAVE_INTERVAL:
                return
            self._write_run(run)
            self._dirty = False
            self._last_save = now

    def current_id(self) -> str:
        with self._lock:
            return str((self.current or {}).get("id") or "")

    def render_current(self) -> str:
        with self._lock:
            if self.current is None:
                return "No search has been logged yet."
            return self._render(self.current)

    def list_runs(self) -> list[dict[str, str]]:
        self.save(force=True)
        items: list[dict[str, str]] = []
        current_id = self.current_id()
        for path in sorted(LOG_DIR.glob("scan_*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            run_id = str(data.get("id") or path.stem.replace("scan_", ""))
            status = str(data.get("status") or "")
            if run_id == current_id and self.current:
                status = str(self.current.get("status") or status)
            started = _stamp_label(str(data.get("started_at") or ""))
            title = str(data.get("title") or "Search")
            line_count = len(data.get("lines") or [])
            items.append(
                {
                    "id": run_id,
                    "label": f"{started}  {title}  ·  {status}  ·  {line_count} lines",
                    "status": status,
                }
            )
        return items

    def render_run(self, run_id: str) -> str:
        if run_id and run_id == self.current_id():
            return self.render_current()
        path = LOG_DIR / f"scan_{run_id}.json"
        if not path.exists():
            return "That log was not found."
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "Could not read that log file."
        if not isinstance(data, dict):
            return "That log file is not valid."
        return self._render(data)

    def _render(self, run: dict[str, Any]) -> str:
        started = _stamp_label(str(run.get("started_at") or ""))
        finished = _stamp_label(str(run.get("finished_at") or ""))
        header = [
            f"{run.get('title') or 'Search'}  ·  {run.get('status') or ''}",
            f"Started  {started}" + (f"    Finished  {finished}" if finished else "    (still running, saved as you go)"),
        ]
        if run.get("detail"):
            header.append(str(run["detail"]))
        header.append("")
        body = []
        for item in run.get("lines") or []:
            if not isinstance(item, dict):
                continue
            ts = _stamp_label(str(item.get("ts") or ""))
            text = str(item.get("text") or "").strip()
            if text:
                body.append(f"{ts}  {text}")
        return "\n".join(header + body)

    def _write_run(self, run: dict[str, Any]) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"scan_{run['id']}.json"
        payload = {
            "id": run["id"],
            "started_at": run.get("started_at") or "",
            "finished_at": run.get("finished_at") or "",
            "title": run.get("title") or "Search",
            "detail": run.get("detail") or "",
            "status": run.get("status") or "",
            "lines": run.get("lines") or [],
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _prune(self) -> None:
        files = sorted(LOG_DIR.glob("scan_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[MAX_RUNS:]:
            try:
                path.unlink()
            except OSError:
                pass
