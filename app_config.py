"""Load and save SISU settings: browser choice and publisher websites."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Iterable

from publisher_sites import builtin_publisher_entries, publishers_match, resolve_builtin_publisher_site

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

BROWSERS: tuple[tuple[str, str], ...] = (
    ("chrome", "Google Chrome"),
    ("edge", "Microsoft Edge"),
    ("firefox", "Mozilla Firefox"),
    ("brave", "Brave"),
    ("system", "System default"),
    ("custom", "Custom executable…"),
)

_cache: dict | None = None
_mtime: float = 0.0


def _defaults() -> dict:
    return {
        "browser": "chrome",
        "browser_path": "",
        "publishers": {},
        "excel_dir": "",
    }


def normalize_site_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def browser_label(browser_id: str) -> str:
    for key, label in BROWSERS:
        if key == browser_id:
            return label
    return "browser"


def load_config() -> dict:
    global _cache, _mtime
    if CONFIG_PATH.exists():
        stamp = CONFIG_PATH.stat().st_mtime
        if _cache is not None and stamp == _mtime:
            return _cache
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        data = _normalize(raw if isinstance(raw, dict) else {})
        _cache = data
        _mtime = stamp
        return data
    _cache = _defaults()
    _mtime = 0.0
    return _cache


def save_config(data: dict) -> Path:
    global _cache, _mtime
    payload = _normalize(data)
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _cache = payload
    _mtime = CONFIG_PATH.stat().st_mtime
    return CONFIG_PATH


def _normalize(raw: dict) -> dict:
    data = _defaults()
    browser = str(raw.get("browser") or "chrome").strip().lower()
    if browser not in {key for key, _label in BROWSERS}:
        browser = "chrome"
    data["browser"] = browser
    data["browser_path"] = str(raw.get("browser_path") or "").strip()
    excel_dir = str(raw.get("excel_dir") or "").strip()
    data["excel_dir"] = excel_dir
    publishers: dict[str, str] = {}
    incoming = raw.get("publishers") or {}
    if isinstance(incoming, dict):
        for name, url in incoming.items():
            label = str(name or "").strip()
            if not label:
                continue
            publishers[label] = normalize_site_url(str(url or ""))
    data["publishers"] = publishers
    return data


def configured_publisher_site(publisher: str) -> str | None:
    mapping = load_config().get("publishers") or {}
    from publisher_sites import _haystack, _norm

    hay = _haystack(publisher)
    if hay == "  ":
        return None
    best_url = None
    best_len = 0
    for name, url in mapping.items():
        site = str(url or "").strip()
        if not site:
            continue
        if _haystack(name).strip() == hay.strip():
            return site
        needle = f" {_norm(name)} "
        if needle in hay and len(needle) > best_len:
            best_url = site
            best_len = len(needle)
    return best_url


def merged_publisher_rows(extra_names: Iterable[str] | None = None) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: list[str] = []

    def add(name: str, url: str) -> None:
        label = (name or "").strip()
        if not label:
            return
        for index, existing in enumerate(seen):
            if publishers_match(existing, label):
                if url and not rows[index][1]:
                    rows[index] = (existing, url)
                return
        seen.append(label)
        rows.append((label, url))

    user_map: dict[str, str] = load_config().get("publishers") or {}
    for name, url in builtin_publisher_entries():
        add(name, configured_publisher_site(name) or url)
    for name, url in user_map.items():
        add(name, url)
    for name in extra_names or []:
        site = configured_publisher_site(name) or resolve_builtin_publisher_site(name) or ""
        add(name, site)
    rows.sort(key=lambda item: ((0 if not item[1] else 1), item[0]))
    return rows


def browser_executable(browser_id: str, custom_path: str = "") -> str | None:
    if browser_id == "custom":
        path = Path(custom_path).expanduser()
        return str(path) if custom_path and path.exists() else None
    if browser_id == "system":
        return None
    candidates: list[str] = []
    if browser_id == "chrome":
        candidates = [
            shutil.which("chrome") or "",
            shutil.which("chrome.exe") or "",
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    elif browser_id == "edge":
        candidates = [
            shutil.which("msedge") or "",
            shutil.which("msedge.exe") or "",
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ]
    elif browser_id == "firefox":
        candidates = [
            shutil.which("firefox") or "",
            shutil.which("firefox.exe") or "",
            os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"),
        ]
    elif browser_id == "brave":
        candidates = [
            shutil.which("brave") or "",
            shutil.which("brave.exe") or "",
            os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None
