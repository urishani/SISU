"""Call a configured LLM to transliterate Hebrew titles, tracking token use."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

from app_config import LLM_DEFAULT_BASE_URLS, llm_config, update_llm_config
from hebrew_text import has_hebrew, hebrew_phonetic, repair_text

BATCH_SIZE = 20
WARN_RATIOS = (80, 95, 100)
_PHONETIC_SYSTEM = (
    "You transliterate Hebrew book titles into Latin letters (phonetic English spelling). "
    "Do not translate meaning. Keep any already-Latin words as they are. "
    "Use Title Case. Reply with JSON only: a list of strings in the same order as the titles."
)


@dataclass
class PhoneticLlmReport:
    attempted: int = 0
    succeeded: int = 0
    fallback: int = 0
    tokens: int = 0
    skipped_limit: bool = False
    error: str = ""
    warning: str = ""
    llm_indexes: list[int] = field(default_factory=list)


last_phonetic_report = PhoneticLlmReport()


def llm_allowed() -> bool:
    cfg = llm_config()
    return bool(cfg.get("enabled") and str(cfg.get("api_key") or "").strip() and str(cfg.get("model") or "").strip())


def tokens_remaining(cfg: dict | None = None) -> int | None:
    data = cfg or llm_config()
    limit = int(data.get("token_limit") or 0)
    if limit <= 0:
        return None
    used = int(data.get("tokens_used") or 0)
    return max(0, limit - used)


def usage_sentence(cfg: dict | None = None) -> str:
    data = cfg or llm_config()
    used = int(data.get("tokens_used") or 0)
    limit = int(data.get("token_limit") or 0)
    last = int(data.get("last_total_tokens") or 0)
    parts: list[str] = []
    if limit <= 0:
        parts.append(f"{used:,} tokens used (no SISU cap — the service may still limit you).")
    else:
        left = max(0, limit - used)
        percent = min(100, int(used * 100 / limit)) if limit else 0
        parts.append(f"{used:,} of {limit:,} tokens used ({left:,} left, {percent}%).")
    if last:
        parts.append(f"Last call used {last:,} tokens.")
    error = str(data.get("last_error") or "").strip()
    if error:
        parts.append("Last error: " + error)
    return " ".join(parts)


def can_call_llm(estimate: int = 0) -> tuple[bool, str]:
    cfg = llm_config()
    if not cfg.get("enabled"):
        return False, ""
    if not str(cfg.get("api_key") or "").strip():
        return False, "LLM is allowed but no API key is set."
    if not str(cfg.get("model") or "").strip():
        return False, "LLM is allowed but no model is set."
    limit = int(cfg.get("token_limit") or 0)
    used = int(cfg.get("tokens_used") or 0)
    if limit > 0 and used >= limit:
        return False, f"LLM token limit reached ({used:,} of {limit:,}). Phonetic titles will use the built-in spelling."
    if limit > 0 and estimate and used + estimate > limit:
        return False, f"Not enough tokens left for this LLM call ({max(0, limit - used):,} remaining)."
    return True, ""


def record_usage(prompt_tokens: int, completion_tokens: int, error: str = "") -> dict:
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    total = prompt_tokens + completion_tokens
    cfg = llm_config()
    used = int(cfg.get("tokens_used") or 0) + total
    changes: dict[str, object] = {
        "tokens_used": used,
        "last_prompt_tokens": prompt_tokens,
        "last_completion_tokens": completion_tokens,
        "last_total_tokens": total,
        "last_error": (error or "").strip(),
    }
    return update_llm_config(**changes)


def usage_warning(cfg: dict | None = None) -> str:
    data = cfg or llm_config()
    limit = int(data.get("token_limit") or 0)
    if limit <= 0:
        return ""
    used = int(data.get("tokens_used") or 0)
    percent = int(used * 100 / limit) if limit else 0
    already = int(data.get("warned_ratio") or 0)
    hit = 0
    for ratio in WARN_RATIOS:
        if percent >= ratio:
            hit = ratio
    if hit and hit > already:
        update_llm_config(warned_ratio=hit)
        if hit >= 100:
            return (
                f"LLM token limit reached ({used:,} of {limit:,}). "
                "Further phonetic titles will use the built-in spelling until you raise the limit or reset usage."
            )
        left = max(0, limit - used)
        return (
            f"LLM usage is at {hit}% of the token limit ({used:,} of {limit:,}, {left:,} left). "
            "Phonetic generation will stop using the LLM when the cap is reached."
        )
    return ""


def phonetic_titles(titles: list[str], *, allow_llm: bool = True) -> list[str]:
    """Return a phonetic spelling for each Hebrew title. Falls back to the built-in spelling."""
    cleaned = [repair_text(title) for title in titles]
    out = [hebrew_phonetic(title) if has_hebrew(title) else "" for title in cleaned]
    if not allow_llm:
        return out
    indexes = [index for index, title in enumerate(cleaned) if has_hebrew(title)]
    if not indexes:
        return out
    allowed, reason = can_call_llm()
    report = PhoneticLlmReport(attempted=len(indexes), fallback=len(indexes), error=reason)
    global last_phonetic_report
    last_phonetic_report = report
    if not allowed:
        report.skipped_limit = "limit" in reason.lower()
        return out
    report.error = ""
    tokens_this_pass = 0
    for start in range(0, len(indexes), BATCH_SIZE):
        chunk_indexes = indexes[start : start + BATCH_SIZE]
        estimate = max(80, 40 * len(chunk_indexes))
        allowed, reason = can_call_llm(estimate)
        if not allowed:
            report.error = reason
            report.skipped_limit = "limit" in reason.lower()
            break
        chunk_titles = [cleaned[index] for index in chunk_indexes]
        try:
            spelled, prompt_tokens, completion_tokens = _complete_phonetics(chunk_titles)
        except Exception as exc:
            report.error = str(exc).strip() or "LLM request failed."
            record_usage(0, 0, report.error)
            break
        record_usage(prompt_tokens, completion_tokens)
        tokens_this_pass += prompt_tokens + completion_tokens
        for offset, index in enumerate(chunk_indexes):
            value = spelled[offset] if offset < len(spelled) else ""
            if value and not has_hebrew(value):
                out[index] = value
                report.succeeded += 1
                report.fallback -= 1
                report.llm_indexes.append(index)
    report.tokens = tokens_this_pass
    report.warning = usage_warning()
    last_phonetic_report = report
    return out


def phonetic_title(title: str, *, allow_llm: bool = True) -> str:
    results = phonetic_titles([title], allow_llm=allow_llm)
    return results[0] if results else ""


def test_connection() -> tuple[bool, str]:
    allowed, reason = can_call_llm()
    if not allowed:
        return False, reason or "LLM is not configured."
    try:
        spelled, prompt_tokens, completion_tokens = _complete_phonetics(["שלום"])
    except Exception as exc:
        record_usage(0, 0, str(exc).strip())
        return False, str(exc).strip() or "LLM request failed."
    cfg = record_usage(prompt_tokens, completion_tokens)
    sample = spelled[0] if spelled else ""
    if not sample:
        return False, "The model replied but did not return a phonetic title."
    warning = usage_warning(cfg)
    extra = f" {warning}" if warning else ""
    return True, f"Connected. Sample: {sample}. {usage_sentence(cfg)}{extra}"


def _complete_phonetics(titles: list[str]) -> tuple[list[str], int, int]:
    numbered = "\n".join(f"{index + 1}. {title}" for index, title in enumerate(titles))
    user = f"Transliterate these Hebrew book titles:\n{numbered}"
    content, prompt_tokens, completion_tokens = _chat(_PHONETIC_SYSTEM, user)
    parsed = _parse_phonetics(content, len(titles))
    return parsed, prompt_tokens, completion_tokens


def _chat(system: str, user: str) -> tuple[str, int, int]:
    cfg = llm_config()
    service = str(cfg.get("service") or "openai")
    if service == "anthropic":
        return _chat_anthropic(cfg, system, user)
    if service == "google":
        return _chat_google(cfg, system, user)
    return _chat_openai(cfg, system, user)


def _chat_openai(cfg: dict, system: str, user: str) -> tuple[str, int, int]:
    base = str(cfg.get("base_url") or "").strip().rstrip("/") or LLM_DEFAULT_BASE_URLS.get(
        str(cfg.get("service") or ""), ""
    )
    if not base:
        raise RuntimeError("Set an API base URL for this LLM service.")
    url = base if base.endswith("/chat/completions") else base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.get('api_key')}",
        "Content-Type": "application/json",
    }
    if str(cfg.get("service") or "") == "openrouter":
        headers["HTTP-Referer"] = "https://local.sisu"
        headers["X-Title"] = "SISU"
    payload = {
        "model": cfg.get("model"),
        "temperature": 0,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = _post_json(url, headers, payload)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(_api_error_message(data) or "The LLM returned no choices.")
    message = choices[0].get("message") or {}
    content = str(message.get("content") or "")
    usage = data.get("usage") or {}
    prompt_tokens, completion_tokens = _openai_usage(usage)
    return content, prompt_tokens, completion_tokens


def _chat_anthropic(cfg: dict, system: str, user: str) -> tuple[str, int, int]:
    base = str(cfg.get("base_url") or "").strip().rstrip("/") or LLM_DEFAULT_BASE_URLS["anthropic"]
    if base.endswith("/messages"):
        url = base
    elif base.endswith("/v1"):
        url = base + "/messages"
    else:
        url = base + "/v1/messages"
    headers = {
        "x-api-key": str(cfg.get("api_key") or ""),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": cfg.get("model"),
        "max_tokens": 1024,
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    data = _post_json(url, headers, payload)
    blocks = data.get("content") or []
    parts = [str(block.get("text") or "") for block in blocks if isinstance(block, dict)]
    content = "\n".join(part for part in parts if part)
    if not content:
        raise RuntimeError(_api_error_message(data) or "The LLM returned no text.")
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    return content, prompt_tokens, completion_tokens


def _chat_google(cfg: dict, system: str, user: str) -> tuple[str, int, int]:
    base = str(cfg.get("base_url") or "").strip().rstrip("/") or LLM_DEFAULT_BASE_URLS["google"]
    model = str(cfg.get("model") or "").strip()
    url = f"{base}/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
    }
    data = _post_json(url, {"Content-Type": "application/json"}, payload, params={"key": cfg.get("api_key")})
    candidates = data.get("candidates") or []
    parts: list[str] = []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = str(part.get("text") or "")
            if text:
                parts.append(text)
    text = "\n".join(parts)
    if not text:
        raise RuntimeError(_api_error_message(data) or "The LLM returned no text.")
    usage = data.get("usageMetadata") or {}
    prompt_tokens = int(usage.get("promptTokenCount") or 0)
    completion_tokens = int(usage.get("candidatesTokenCount") or 0)
    return text, prompt_tokens, completion_tokens


def _openai_usage(usage: dict) -> tuple[int, int]:
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    if total and not (prompt_tokens or completion_tokens):
        return total, 0
    return prompt_tokens, completion_tokens


def _post_json(url: str, headers: dict, payload: dict, params: dict | None = None) -> dict:
    try:
        response = requests.post(url, headers=headers, params=params, json=payload, timeout=45)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach the LLM service: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        raise RuntimeError(_api_error_message(data) or f"LLM HTTP {response.status_code}")
    if not isinstance(data, dict):
        raise RuntimeError("The LLM returned an unexpected response.")
    return data


def _api_error_message(data: dict) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "").strip()
    if isinstance(error, str):
        return error.strip()
    return str(data.get("message") or "").strip()


def _parse_phonetics(text: str, count: int) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return [""] * count
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        lines = [re.sub(r"^\d+[\).\s-]+", "", line).strip() for line in raw.splitlines() if line.strip()]
        data = lines
    if isinstance(data, dict):
        if isinstance(data.get("phonetics"), list):
            data = data.get("phonetics")
        else:
            ordered: list[str] = []
            for index in range(1, count + 1):
                ordered.append(str(data.get(str(index)) or data.get(index) or "").strip())
            if any(ordered):
                data = ordered
            else:
                data = [str(value or "").strip() for value in data.values()]
    if not isinstance(data, list):
        return [""] * count
    values = [repair_text(str(item or "")).strip() for item in data]
    if len(values) < count:
        values.extend([""] * (count - len(values)))
    return values[:count]


def phonetic_status_note() -> str:
    report = last_phonetic_report
    parts: list[str] = []
    if report.succeeded:
        parts.append(f"{report.succeeded:,} via LLM")
    if report.tokens:
        parts.append(f"{report.tokens:,} tokens this pass")
    cfg = llm_config()
    remaining = tokens_remaining(cfg)
    if remaining is not None:
        parts.append(f"{remaining:,} tokens left")
    elif report.tokens or report.succeeded:
        used = int(cfg.get("tokens_used") or 0)
        parts.append(f"{used:,} tokens used, no SISU cap")
    if report.skipped_limit:
        parts.append("LLM stopped at the token limit")
    elif report.error and not report.succeeded:
        parts.append(report.error)
    return "; ".join(parts)
