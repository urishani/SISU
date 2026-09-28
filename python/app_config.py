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
# Bump this to fill menukad titles and nikud-based phonetics once more on existing books.
NIKUD_FILL_ID = 1

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
        "publisher_preferred": {},
        "search_sites": _default_search_sites(),
        "excel_dir": "",
        "nli_api_key": "",
        "llm": _llm_defaults(),
        "phonetic_model_prompted_id": 0,
        "nikud_fill_id": 0,
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
    data["publisher_preferred"] = _clean_publisher_preferred(raw.get("publisher_preferred"))
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
    if "nikud_fill_id" in raw:
        nikud_raw = raw.get("nikud_fill_id")
    else:
        nikud_raw = previous.get("nikud_fill_id")
    try:
        data["nikud_fill_id"] = max(0, int(nikud_raw or 0))
    except (TypeError, ValueError):
        data["nikud_fill_id"] = 0
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


def nikud_fill_pending() -> bool:
    data = load_config()
    try:
        filled = int(data.get("nikud_fill_id") or 0)
    except (TypeError, ValueError):
        filled = 0
    return filled < NIKUD_FILL_ID


def mark_nikud_filled() -> None:
    data = load_config()
    data["nikud_fill_id"] = NIKUD_FILL_ID
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


def configured_publisher_site(
    publisher: str,
    mapping: dict[str, str] | None = None,
) -> str | None:
    if mapping is None:
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


def publisher_is_preferred(publisher: str, flags: dict | None = None) -> bool:
    """Publishers are preferred unless a saved flag says otherwise.

    The longest matching publisher name wins, so a specific row can differ
    from a shorter name that also matches.
    """
    if flags is None:
        raw = load_config().get("publisher_preferred") or {}
        flags = raw if isinstance(raw, dict) else {}
    if not flags:
        return True
    from publisher_sites import _haystack

    hay = _haystack(publisher).strip()
    best_len = -1
    best: bool | None = None
    for name, preferred in flags.items():
        label = str(name or "").strip()
        if not label:
            continue
        other = _haystack(label).strip()
        if hay and other == hay:
            return _pref_bool(preferred)
        if hay and other and publishers_match(label, publisher) and len(other) > best_len:
            best_len = len(other)
            best = _pref_bool(preferred)
    if best is not None:
        return best
    return True


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
        add(name, configured_publisher_site(name, user_map) or url)
    for name, url in user_map.items():
        add(name, url)
    for name in extra_names or []:
        site = configured_publisher_site(name, user_map) or resolve_builtin_publisher_site(name) or ""
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


SETTINGS_PACK_KIND = "sisu-settings"
SETTINGS_PACK_VERSION = 1


def _settings_site_key(url: str) -> str:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(normalize_site_url(url))
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path).rstrip("/") or "/"
    return f"{host}{path}".casefold()


def _clean_settings_sites(raw: object) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    items = raw if isinstance(raw, list) else []
    for item in items:
        if isinstance(item, str):
            url = normalize_site_url(item)
            enabled = True
        elif isinstance(item, dict):
            url = normalize_site_url(str(item.get("url") or ""))
            enabled = bool(item.get("enabled", True))
        else:
            continue
        key = _settings_site_key(url)
        if not url or not key or key in seen:
            continue
        seen.add(key)
        rows.append({"url": url, "enabled": enabled})
    return rows


def _pref_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _publisher_url_value(value: object) -> str:
    if isinstance(value, dict):
        return normalize_site_url(str(value.get("url") or ""))
    return normalize_site_url(str(value or ""))


def _clean_settings_publishers(raw: object) -> dict[str, str]:
    mapping: dict[str, str] = {}
    incoming = raw if isinstance(raw, dict) else {}
    for name, url in incoming.items():
        label = str(name or "").strip()
        if not label:
            continue
        mapping[label] = _publisher_url_value(url)
    return mapping


def _clean_publisher_preferred(raw: object, publishers: object = None) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    incoming = raw if isinstance(raw, dict) else {}
    for name, value in incoming.items():
        label = str(name or "").strip()
        if not label:
            continue
        flags[label] = _pref_bool(value)
    nested = publishers if isinstance(publishers, dict) else {}
    for name, value in nested.items():
        label = str(name or "").strip()
        if not label or not isinstance(value, dict) or "preferred" not in value:
            continue
        flags[label] = _pref_bool(value.get("preferred"))
    return flags


def build_settings_pack() -> dict:
    data = load_config()
    from field_map import load_alias_payload

    aliases = load_alias_payload()
    return {
        "kind": SETTINGS_PACK_KIND,
        "version": SETTINGS_PACK_VERSION,
        "search_sites": _clean_settings_sites(data.get("search_sites")),
        "publishers": _clean_settings_publishers(data.get("publishers")),
        "publisher_preferred": _clean_publisher_preferred(data.get("publisher_preferred")),
        "aliases": dict(aliases.get("aliases") or {}),
        "cover_values": dict(aliases.get("cover_values") or {}),
    }


def settings_pack_counts(pack: dict | None) -> dict[str, int]:
    data = pack if isinstance(pack, dict) else {}
    aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    covers = data.get("cover_values") if isinstance(data.get("cover_values"), dict) else {}
    publishers = data.get("publishers") if isinstance(data.get("publishers"), dict) else {}
    return {
        "sites": len(_clean_settings_sites(data.get("search_sites"))),
        "publishers": len(_clean_settings_publishers(publishers)),
        "aliases": len(aliases),
        "covers": len(covers),
    }


def settings_pack_has_data(pack: dict | None) -> bool:
    counts = settings_pack_counts(pack)
    return any(counts[key] for key in ("sites", "publishers", "aliases", "covers"))


def _extract_json_object(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return raw[start : end + 1]
    return raw


def parse_settings_pack(source: str | dict | None) -> dict | None:
    if isinstance(source, dict):
        data = source
    else:
        text = _extract_json_object(str(source or ""))
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    covers = data.get("cover_values") if isinstance(data.get("cover_values"), dict) else {}
    nested = data.get("field_aliases") if isinstance(data.get("field_aliases"), dict) else {}
    if not aliases and isinstance(nested.get("aliases"), dict):
        aliases = nested.get("aliases") or {}
    if not covers and isinstance(nested.get("cover_values"), dict):
        covers = nested.get("cover_values") or {}
    pack = {
        "kind": SETTINGS_PACK_KIND,
        "version": SETTINGS_PACK_VERSION,
        "search_sites": _clean_settings_sites(data.get("search_sites")),
        "publishers": _clean_settings_publishers(data.get("publishers")),
        "publisher_preferred": _clean_publisher_preferred(
            data.get("publisher_preferred"),
            data.get("publishers"),
        ),
        "aliases": {
            str(label).strip(): str(field).strip()
            for label, field in aliases.items()
            if str(label).strip() and str(field).strip()
        },
        "cover_values": {
            str(label).strip(): str(code).strip().upper()
            for label, code in covers.items()
            if str(label).strip() and str(code).strip()
        },
    }
    if not settings_pack_has_data(pack):
        return None
    return pack


def merge_settings_pack(pack: dict | None) -> dict[str, int]:
    parsed = parse_settings_pack(pack)
    if not parsed:
        return {
            "sites_added": 0,
            "sites_skipped": 0,
            "publishers_added": 0,
            "publishers_updated": 0,
            "aliases_added": 0,
            "aliases_updated": 0,
            "covers_added": 0,
            "covers_updated": 0,
        }
    current = load_config()
    sites = _clean_settings_sites(current.get("search_sites"))
    known = {_settings_site_key(str(item.get("url") or "")) for item in sites}
    sites_added = sites_skipped = 0
    for item in parsed.get("search_sites") or []:
        url = str(item.get("url") or "")
        key = _settings_site_key(url)
        if not url or not key:
            continue
        if key in known:
            sites_skipped += 1
            continue
        known.add(key)
        sites.append({"url": url, "enabled": bool(item.get("enabled", True))})
        sites_added += 1
    publishers = _clean_settings_publishers(current.get("publishers"))
    preferred = _clean_publisher_preferred(current.get("publisher_preferred"))
    incoming_preferred = _clean_publisher_preferred(parsed.get("publisher_preferred"))
    pub_added = pub_updated = 0
    for name, url in (parsed.get("publishers") or {}).items():
        label = str(name or "").strip()
        site = normalize_site_url(str(url or ""))
        if not label:
            continue
        matched = next((existing for existing in publishers if publishers_match(existing, label) or existing == label), None)
        flag_key = label if label in incoming_preferred else matched
        changed_pref = False
        if flag_key and flag_key in incoming_preferred:
            target = matched or label
            flag = incoming_preferred[flag_key]
            if preferred.get(target, True) != flag:
                preferred[target] = flag
                changed_pref = True
        if matched is None:
            publishers[label] = site
            pub_added += 1
        elif (site and publishers.get(matched) != site) or changed_pref:
            if site and publishers.get(matched) != site:
                publishers[matched] = site
            pub_updated += 1
    current["search_sites"] = sites
    current["publishers"] = publishers
    current["publisher_preferred"] = preferred
    save_config(current)
    from field_map import merge_alias_payload

    alias_counts = merge_alias_payload(parsed.get("aliases") or {}, parsed.get("cover_values") or {})
    return {
        "sites_added": sites_added,
        "sites_skipped": sites_skipped,
        "publishers_added": pub_added,
        "publishers_updated": pub_updated,
        **alias_counts,
    }
