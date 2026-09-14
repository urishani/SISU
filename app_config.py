"""Load and save SISU settings: browser choice, catalog sites, publisher websites, and LLM."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Iterable

from publisher_sites import builtin_publisher_entries, publishers_match, resolve_builtin_publisher_site

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_SEARCH_URLS = (
    "https://www.booknet.co.il/ספרים-חדשים",
    "https://www.e-vrit.co.il/group/3/ספרים-חדשים",
    "https://www.nli.org.il/he/search?materialType=books",
)
PLACEHOLDER_URL_MARKERS = ("example.com", "example.org", "a.example")

# Bump this if a later phonetic model should offer one more reapply of existing titles.
PHONETIC_MODEL_PROMPT_ID = 1

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
_lock = threading.RLock()

LLM_SERVICES: tuple[tuple[str, str], ...] = (
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("google", "Google Gemini"),
    ("groq", "Groq"),
    ("openrouter", "OpenRouter"),
    ("custom", "Custom (OpenAI-compatible)"),
)

LLM_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "google": "gemini-2.0-flash",
    "groq": "llama-3.1-8b-instant",
    "openrouter": "openai/gpt-4o-mini",
    "custom": "",
}

LLM_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "custom": "",
}


def _llm_defaults() -> dict:
    return {
        "enabled": False,
        "service": "openai",
        "api_key": "",
        "model": LLM_DEFAULT_MODELS["openai"],
        "base_url": "",
        "token_limit": 0,
        "tokens_used": 0,
        "last_prompt_tokens": 0,
        "last_completion_tokens": 0,
        "last_total_tokens": 0,
        "last_error": "",
        "warned_ratio": 0,
    }


def _default_search_sites() -> list[dict]:
    return [{"url": url, "enabled": True} for url in DEFAULT_SEARCH_URLS]


def _defaults() -> dict:
    return {
        "browser": "chrome",
        "browser_path": "",
        "publishers": {},
        "search_sites": _default_search_sites(),
        "excel_dir": "",
        "nli_api_key": "",
        "llm": _llm_defaults(),
        "phonetic_model_prompted_id": 0,
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
    with _lock:
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
    with _lock:
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
    data["nli_api_key"] = str(raw.get("nli_api_key") or "").strip()
    publishers: dict[str, str] = {}
    incoming = raw.get("publishers") or {}
    if isinstance(incoming, dict):
        for name, url in incoming.items():
            label = str(name or "").strip()
            if not label:
                continue
            publishers[label] = normalize_site_url(str(url or ""))
    data["publishers"] = publishers
    if "search_sites" in raw:
        data["search_sites"] = _normalize_search_sites(raw.get("search_sites"), fallback=False)
    else:
        data["search_sites"] = _default_search_sites()
    data["llm"] = _normalize_llm(raw.get("llm") if isinstance(raw.get("llm"), dict) else {})
    previous = _cache if isinstance(_cache, dict) else {}
    if "phonetic_model_prompted_id" in raw:
        prompted_raw = raw.get("phonetic_model_prompted_id")
    else:
        prompted_raw = previous.get("phonetic_model_prompted_id")
    try:
        data["phonetic_model_prompted_id"] = max(0, int(prompted_raw or 0))
    except (TypeError, ValueError):
        data["phonetic_model_prompted_id"] = 0
    return data


def _is_placeholder_url(url: str) -> bool:
    return any(marker in (url or "").casefold() for marker in PLACEHOLDER_URL_MARKERS)


def _normalize_search_sites(raw: object, *, fallback: bool = True) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    source: list = raw if isinstance(raw, list) else []
    for item in source:
        if isinstance(item, str):
            url, enabled = item, True
        elif isinstance(item, dict):
            url = str(item.get("url") or "")
            enabled = bool(item.get("enabled", True))
        else:
            continue
        url = normalize_site_url(url)
        if not url or _is_placeholder_url(url) or url in seen:
            continue
        seen.add(url)
        items.append({"url": url, "enabled": enabled})
    if items or not fallback:
        return items
    return _default_search_sites()


def search_sites() -> list[dict]:
    value = load_config().get("search_sites") or []
    return [dict(item) for item in value if isinstance(item, dict) and str(item.get("url") or "").strip()]


def all_search_urls() -> list[str]:
    return [str(item.get("url") or "").strip() for item in search_sites() if str(item.get("url") or "").strip()]


def enabled_search_urls() -> list[str]:
    return [
        str(item.get("url") or "").strip()
        for item in search_sites()
        if str(item.get("url") or "").strip() and item.get("enabled", True)
    ]


def update_search_sites(sites: Iterable[dict | str]) -> list[dict]:
    data = load_config()
    data["search_sites"] = _normalize_search_sites(list(sites), fallback=False)
    save_config(data)
    return search_sites()


def _normalize_llm(raw: dict) -> dict:
    data = _llm_defaults()
    data["enabled"] = bool(raw.get("enabled"))
    service = str(raw.get("service") or "openai").strip().lower()
    if service not in {key for key, _label in LLM_SERVICES}:
        service = "openai"
    data["service"] = service
    data["api_key"] = str(raw.get("api_key") or "").strip()
    model = str(raw.get("model") or "").strip()
    data["model"] = model or LLM_DEFAULT_MODELS.get(service, "")
    data["base_url"] = str(raw.get("base_url") or "").strip().rstrip("/")
    try:
        limit = int(raw.get("token_limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    data["token_limit"] = max(0, limit)
    try:
        used = int(raw.get("tokens_used") or 0)
    except (TypeError, ValueError):
        used = 0
    data["tokens_used"] = max(0, used)
    for key in ("last_prompt_tokens", "last_completion_tokens", "last_total_tokens", "warned_ratio"):
        try:
            data[key] = max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            data[key] = 0
    data["last_error"] = str(raw.get("last_error") or "").strip()
    return data


def llm_config() -> dict:
    value = load_config().get("llm") or {}
    return value if isinstance(value, dict) else _llm_defaults()


def phonetic_model_prompt_pending() -> bool:
    data = load_config()
    try:
        prompted = int(data.get("phonetic_model_prompted_id") or 0)
    except (TypeError, ValueError):
        prompted = 0
    return prompted < PHONETIC_MODEL_PROMPT_ID


def mark_phonetic_model_prompted() -> None:
    data = load_config()
    data["phonetic_model_prompted_id"] = PHONETIC_MODEL_PROMPT_ID
    save_config(data)


def update_llm_config(**changes: object) -> dict:
    data = load_config()
    llm = dict(data.get("llm") or _llm_defaults())
    llm.update(changes)
    data["llm"] = llm
    save_config(data)
    return llm_config()


def llm_service_label(service: str) -> str:
    for key, label in LLM_SERVICES:
        if key == service:
            return label
    return service or "LLM"


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
