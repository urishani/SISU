"""Match page labels to catalog fields, harvest unmatched candidates, and write a report."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

APP_DIR = Path(__file__).resolve().parent
ALIASES_PATH = APP_DIR / "field_aliases.json"
CANDIDATES_PATH = APP_DIR / "cache" / "field_candidates.json"
REPORT_JSON_PATH = APP_DIR / "cache" / "field_report.json"
REPORT_MD_PATH = APP_DIR / "cache" / "field_report.md"

EXCEL_TARGETS: dict[str, str] = {
    "supplier": "Supplier",
    "catalog_mom": "Catalog for MOM",
    "cat_number": "cat number short",
    "publisher": "Publisher",
    "title_phonetic": "Title (phonetics)",
    "author_en": "Author (English)",
    "title_en": "Title in English",
    "title_he": "Title in Hebrew",
    "author_he": "Author (Hebrew)",
    "category": "Category #1",
    "category2": "Category #2",
    "category3": "Category #3",
    "upc": "UPC",
    "danacode": "Danacode",
    "isbn": "ISBN",
    "isbn_old": "ISBN -old",
    "item_type": "Item type",
    "language": "Language",
    "translated": "Translated",
    "year": "Copyright year",
    "pages": "Number of pages",
    "cover_type": "Cover type: S;H;BB",
    "spine_color": "Spine Color",
    "weight_lb": "Weight (Labs)",
    "weight_oz": "Weight (Oz)",
    "weight_kg": "Weight (KG)",
    "height_in": "Size- Hight (inc)",
    "width_in": "Size-Width (Inc)",
    "thickness_in": "Size- Thickness (Inc)",
    "height_cm": "Size- Hight (cm)",
    "width_cm": "Size-Width (cm)",
    "thickness_cm": "Size- Thickness (cm)",
    "price_ils": "Israeli price (Shekel)",
    "description_en": "Description (English)",
    "description": "Description (Hebrew)",
    "comments": "comments",
    "keywords": "Keywords",
    "cover_image_url": "Cover image URL",
    "back_image_url": "Back image URL",
    "translator": "Translator",
    "illustrator": "Illustrator",
    "marc": "MARC",
    "ddc": "DDC",
    "scanner_id": "Scanner ID",
}

CORE_FIELDS = {
    "publisher",
    "author",
    "title",
    "year",
    "pages",
    "isbn",
    "danacode",
    "upc",
    "cover_type",
    "weight_kg",
    "height_cm",
    "width_cm",
    "thickness_cm",
    "price_ils",
    "description",
    "dimensions",
    "cover_image_url",
    "back_image_url",
    "translator",
    "illustrator",
    "marc",
    "ddc",
}

LABEL_MAP_KEYS = {
    "cover": "cover_type",
    "weight": "weight_kg",
    "height": "height_cm",
    "width": "width_cm",
    "thickness": "thickness_cm",
    "price": "price_ils",
}

NOISE_LABELS = {
    "add to cart",
    "הוסף לסל",
    "wishlist",
    "share",
    "quantity",
    "כמות",
    "home",
    "cart",
    "login",
    "menu",
    "search",
    "filter",
    "sort",
    "cookie",
}

_alias_index: dict[str, str] | None = None
_candidates: dict[str, dict[str, Any]] | None = None
_dirty = 0


def normalize_label(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    value = value.replace("״", '"').replace("׳", "'").rstrip(":")
    return re.sub(r"[^a-z0-9\u0590-\u05ff]+", "", value)


def site_host(url: str) -> str:
    host = urlparse(url or "").netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host


def load_aliases() -> dict[str, str]:
    global _alias_index
    if _alias_index is not None:
        return _alias_index
    index: dict[str, str] = {}
    if ALIASES_PATH.exists():
        try:
            raw = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        aliases = raw.get("aliases") if isinstance(raw, dict) else {}
        if isinstance(aliases, dict):
            for label, field in aliases.items():
                key = normalize_label(str(label))
                value = str(field or "").strip()
                if key and value:
                    index[key] = value
    _alias_index = index
    return index


_cover_values: dict[str, str] | None = None


def load_cover_values() -> dict[str, str]:
    global _cover_values
    if _cover_values is not None:
        return _cover_values
    values: dict[str, str] = {}
    if ALIASES_PATH.exists():
        try:
            raw = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        mapping = raw.get("cover_values") if isinstance(raw, dict) else {}
        if isinstance(mapping, dict):
            for label, code in mapping.items():
                key = normalize_label(str(label))
                value = str(code or "").strip().upper()
                if key and value in {"S", "H", "BB"}:
                    values[key] = value
    _cover_values = values
    return values


def isolate_language(value: str | None) -> str:
    """Keep only the language itself when extra labels were glued into the same text."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    text = re.split(
        r"\s+(?=שם הספר|שם המחבר|דאנאקוד|דנאקוד|דאנא קוד|ISBN|מסת|הוצאה|מס['׳]?\s*עמודים|סוג כריכה|ת\.\s*הוצאה)",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    text = re.sub(r"[:：].*$", "", text).strip(" :,-")
    if len(text) > 40:
        text = text.split()[0]
    return text


def cover_code(text: str | None) -> str:
    compact = normalize_label(text or "")
    if compact:
        mapped = load_cover_values().get(compact)
        if mapped:
            return mapped
    value = (text or "").lower().strip()
    hebrew = (text or "").strip()
    if hebrew in {"רכה", "רך", "כריכה רכה"} or "כריכה רכה" in hebrew or any(
        word in value for word in ("paperback", "softcover", "soft cover")
    ):
        return "S"
    if hebrew in {"קשה", "כריכה קשה"} or "כריכה קשה" in hebrew or any(
        word in value for word in ("hardcover", "hard cover", "hardback")
    ):
        return "H"
    if "board book" in value or "קרטון" in hebrew:
        return "BB"
    return ""


def reload_aliases() -> None:
    global _alias_index, _cover_values
    _alias_index = None
    _cover_values = None
    load_aliases()


def resolve_label(label: str) -> str | None:
    compact = normalize_label(label)
    if not compact or compact in {normalize_label(item) for item in NOISE_LABELS}:
        return None
    field = load_aliases().get(compact)
    if field:
        return field
    from book_crawler import LABEL_MAP

    for name, aliases in LABEL_MAP.items():
        for alias in aliases:
            if normalize_label(alias) == compact:
                return LABEL_MAP_KEYS.get(name, name)
    return None


def suggest_field(label: str) -> str | None:
    matched = resolve_label(label)
    if matched:
        return matched
    compact = normalize_label(label)
    if len(compact) < 4:
        return None
    best = None
    best_len = 0
    for alias, field in load_aliases().items():
        if len(alias) >= 4 and (alias in compact or compact in alias) and len(alias) > best_len:
            best = field
            best_len = len(alias)
    if best:
        return best
    from book_crawler import LABEL_MAP

    for name, aliases in LABEL_MAP.items():
        field = LABEL_MAP_KEYS.get(name, name)
        for alias in aliases:
            needle = normalize_label(alias)
            if len(needle) >= 4 and (needle in compact or compact in needle) and len(needle) > best_len:
                best = field
                best_len = len(needle)
    for field, header in EXCEL_TARGETS.items():
        needle = normalize_label(header)
        if len(needle) >= 4 and (needle in compact or compact in needle) and len(needle) > best_len:
            best = field
            best_len = len(needle)
    return best


def apply_field(book: Any, field: str, value: str) -> bool:
    from book_crawler import (
        apply_identifier,
        extract_year,
        format_person_name,
        map_cover,
        parse_cm_triplet,
        parse_pages,
        parse_price,
        parse_weight_kg,
        _looks_like_person_name,
    )

    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if not field or not value:
        return False

    def empty(name: str) -> bool:
        return not str(getattr(book, name, "") or "").strip()

    if field == "publisher" and empty("publisher"):
        book.publisher = value
        return True
    if field == "author" and empty("author"):
        book.author = format_person_name(value) or value
        return bool(book.author)
    if field in {"author_en", "author_he"}:
        formatted = format_person_name(value) or value
        captured = book.captured_fields()
        if formatted and not captured.get(field):
            book.set_captured(field, formatted)
            return True
        return False
    if field in {"cover_image_url", "back_image_url"} and empty(field):
        setattr(book, field, value)
        return True
    if field == "title" and empty("title"):
        book.title = value
        return True
    if field == "year" and empty("year"):
        book.year = extract_year(value)
        return bool(book.year)
    if field == "pages" and empty("pages"):
        book.pages = parse_pages(value) or re.sub(r"[^\d]", "", value)
        return bool(book.pages)
    if field == "isbn":
        before = book.isbn
        apply_identifier(book, value)
        return book.isbn != before
    if field == "danacode":
        from book_crawler import remember_danacode

        return bool(remember_danacode(book, value))
    if field == "upc" and empty("upc"):
        book.upc = re.sub(r"\D", "", value)
        return bool(book.upc)
    if field == "cover_type" and empty("cover_type"):
        book.cover_type = cover_code(value) or map_cover(value)
        return bool(book.cover_type)
    if field == "weight_kg" and empty("weight_kg"):
        book.weight_kg = parse_weight_kg(value)
        return bool(book.weight_kg)
    if field == "height_cm" and empty("height_cm"):
        book.height_cm = re.sub(r"[^\d.]", "", value.replace(",", "."))
        return bool(book.height_cm)
    if field == "width_cm" and empty("width_cm"):
        book.width_cm = re.sub(r"[^\d.]", "", value.replace(",", "."))
        return bool(book.width_cm)
    if field == "thickness_cm" and empty("thickness_cm"):
        book.thickness_cm = re.sub(r"[^\d.]", "", value.replace(",", "."))
        return bool(book.thickness_cm)
    if field == "dimensions":
        height, width, thickness = parse_cm_triplet(value)
        changed = False
        if height and empty("height_cm"):
            book.height_cm = height
            changed = True
        if width and empty("width_cm"):
            book.width_cm = width
            changed = True
        if thickness and empty("thickness_cm"):
            book.thickness_cm = thickness
            changed = True
        return changed
    if field == "price_ils" and empty("price_ils"):
        book.price_ils = parse_price(value)
        return bool(book.price_ils)
    if field == "description" and empty("description"):
        book.description = value
        return True
    if field == "translated":
        captured = book.captured_fields()
        if _looks_like_person_name(value) and len(value.split()) >= 2:
            if empty("translator"):
                book.translator = format_person_name(value) or value
            if not captured.get("translated"):
                book.set_captured("translated", "Y")
            return bool(book.translator)
        text = value
        if text and not captured.get("translated"):
            book.set_captured("translated", text)
            return True
        return False
    if field == "translator" and empty("translator"):
        book.translator = format_person_name(value) or value
        captured = book.captured_fields()
        if book.translator and not captured.get("translated"):
            book.set_captured("translated", "Y")
        return bool(book.translator)
    if field == "illustrator" and empty("illustrator"):
        book.illustrator = format_person_name(value) or value
        return bool(book.illustrator)
    if field == "marc" and empty("marc"):
        digits = re.sub(r"\s", "", value)
        book.marc = digits if re.fullmatch(r"\d{6,}", digits) else value.strip()[:80]
        return bool(book.marc)
    if field == "ddc" and empty("ddc"):
        match = re.search(r"\d{1,3}(?:\.\d+)*", value)
        book.ddc = match.group(0) if match else value.strip()[:80]
        return bool(book.ddc)
    if field == "language":
        value = isolate_language(value)
        if not value:
            return False
        captured = book.captured_fields()
        current = captured.get("language") or ""
        cleaned = isolate_language(current)
        if current and current == cleaned:
            return False
        captured["language"] = value
        book._save_map("captured", captured)
        return True
    if field in EXCEL_TARGETS and field not in CORE_FIELDS:
        captured = book.captured_fields()
        if value and not captured.get(field):
            book.set_captured(field, value)
            return True
    return False


def apply_pairs(book: Any, pairs: dict[str, str]) -> list[str]:
    filled: list[str] = []
    for label, value in pairs.items():
        field = resolve_label(label)
        if field and apply_field(book, field, value):
            filled.append(field)
            if book.url:
                book.record_field_source(field, book.url)
    return filled


def attach_page_fields(book: Any, pairs: dict[str, str]) -> None:
    leftover: list[dict[str, str]] = []
    found: list[dict[str, str]] = []
    for label, value in pairs.items():
        field = resolve_label(label) or ""
        if _noise(label, value) and not field:
            continue
        snippet = value[:240]
        found.append({"label": label, "value": snippet, "field": field})
        if field or _noise(label, value):
            continue
        leftover.append({"label": label, "value": snippet})
        if len(leftover) >= 40:
            break
    if found:
        book.extra["found_fields"] = json.dumps(found, ensure_ascii=False)
    if leftover:
        book.extra["page_fields"] = json.dumps(leftover, ensure_ascii=False)


def _noise(label: str, value: str) -> bool:
    compact = normalize_label(label)
    if not compact or len(compact) > 40:
        return True
    if compact in {normalize_label(item) for item in NOISE_LABELS}:
        return True
    if not value or len(value) > 400:
        return True
    if len(value) < 2:
        return True
    return False


def collect_extra_pairs(soup: BeautifulSoup, html: str) -> dict[str, str]:
    pairs: dict[str, str] = {}

    def remember(label: str, value: str) -> None:
        label = re.sub(r"\s+", " ", str(label or "")).strip().rstrip(":")
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if label and value and label not in pairs:
            pairs[label] = value

    for row in soup.select(".flex"):
        if not isinstance(row, Tag):
            continue
        value_el = row.select_one(".meta-value")
        label_el = row.find(["b", "strong"])
        if label_el and value_el:
            remember(label_el.get_text(" ", strip=True), value_el.get_text(" ", strip=True))
    for row in soup.select("table tr, .product-attribute-specs-table tr, .additional-attributes-wrapper tr"):
        if not isinstance(row, Tag):
            continue
        header = row.find(["th", "td"])
        cells = row.find_all("td")
        if header and cells:
            label = header.get_text(" ", strip=True)
            value = cells[-1].get_text(" ", strip=True) if header.name == "th" else ""
            if header.name == "td" and len(cells) >= 2:
                label = cells[0].get_text(" ", strip=True)
                value = cells[1].get_text(" ", strip=True)
            remember(label, value)
    for tag in soup.select("[itemprop]"):
        prop = str(tag.get("itemprop") or "").strip()
        if prop.casefold() in {"image", "url", "availability", "itemcondition", "position", "logo"}:
            continue
        value = str(tag.get("content") or tag.get_text(" ", strip=True) or "").strip()
        remember(prop, value)
    from book_crawler import json_ld_objects, schema_name

    for item in json_ld_objects(html):
        extra = item.get("additionalProperty") or item.get("additionalProperties")
        props = extra if isinstance(extra, list) else [extra] if extra else []
        for prop in props:
            if not isinstance(prop, dict):
                continue
            remember(schema_name(prop.get("name") or prop.get("propertyID")), schema_name(prop.get("value")))
    return pairs


def remember_candidates(pairs: dict[str, str], url: str) -> None:
    global _dirty
    store = _load_candidates()
    host = site_host(url)
    for label, value in pairs.items():
        if _noise(label, value):
            continue
        key = normalize_label(label)
        if not key:
            continue
        entry = store.setdefault(
            key,
            {
                "label": label,
                "count": 0,
                "hosts": {},
                "samples": [],
                "urls": [],
                "matched_field": resolve_label(label) or "",
            },
        )
        entry["count"] = int(entry.get("count") or 0) + 1
        if len(label) > len(str(entry.get("label") or "")):
            entry["label"] = label
        hosts = entry.setdefault("hosts", {})
        hosts[host] = int(hosts.get(host) or 0) + 1
        samples: list[str] = entry.setdefault("samples", [])
        snippet = value[:120]
        if snippet not in samples and len(samples) < 8:
            samples.append(snippet)
        urls: list[str] = entry.setdefault("urls", [])
        if url and url not in urls and len(urls) < 6:
            urls.append(url)
        if not entry.get("matched_field"):
            entry["matched_field"] = resolve_label(label) or ""
        _dirty += 1
    if _dirty >= 25:
        flush_candidates()


def flush_candidates() -> None:
    global _dirty, _candidates
    if _candidates is None:
        return
    CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATES_PATH.write_text(
        json.dumps({"labels": _candidates}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _dirty = 0


def _load_candidates() -> dict[str, dict[str, Any]]:
    global _candidates
    if _candidates is not None:
        return _candidates
    if CANDIDATES_PATH.exists():
        try:
            raw = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        labels = raw.get("labels") if isinstance(raw, dict) else {}
        _candidates = labels if isinstance(labels, dict) else {}
    else:
        _candidates = {}
    return _candidates


def write_field_report(excel_path: str | Path | None = None) -> Path:
    flush_candidates()
    reload_aliases()
    from catalog_excel import CatalogWorkbook

    excel_rows: list[dict[str, Any]] = []
    if excel_path and Path(excel_path).exists():
        catalog = CatalogWorkbook(excel_path)
        for column in catalog.all_columns:
            excel_rows.append(
                {
                    "header": column["header"],
                    "field": column["field"],
                    "colored": column["colored"],
                    "letter": column["letter"],
                }
            )
    else:
        for field, header in EXCEL_TARGETS.items():
            excel_rows.append({"header": header, "field": field, "colored": field in CORE_FIELDS, "letter": ""})

    candidates = []
    for entry in sorted(_load_candidates().values(), key=lambda item: (-int(item.get("count") or 0), item.get("label") or "")):
        label = str(entry.get("label") or "")
        matched = str(entry.get("matched_field") or resolve_label(label) or "")
        suggested = matched or (suggest_field(label) or "")
        status = "matched" if matched else "suggested" if suggested else "unmatched"
        candidates.append(
            {
                "label": label,
                "count": int(entry.get("count") or 0),
                "hosts": entry.get("hosts") or {},
                "samples": entry.get("samples") or [],
                "urls": entry.get("urls") or [],
                "matched_field": matched,
                "suggested_field": suggested if not matched else "",
                "status": status,
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alias_file": str(ALIASES_PATH),
        "excel_targets": excel_rows,
        "candidates": candidates,
        "counts": {
            "targets": len(excel_rows),
            "candidates": len(candidates),
            "matched": sum(1 for item in candidates if item["status"] == "matched"),
            "suggested": sum(1 for item in candidates if item["status"] == "suggested"),
            "unmatched": sum(1 for item in candidates if item["status"] == "unmatched"),
        },
    }
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_MD_PATH.write_text(_render_markdown(payload), encoding="utf-8")
    return REPORT_MD_PATH


def _render_markdown(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") or {}
    lines = [
        "# Field matching report",
        "",
        f"Generated: {payload.get('generated_at')}",
        "",
        "This report lists catalog properties from the Excel file and labels harvested from bookstore/publisher pages.",
        "Unmatched labels are candidates for the conversion table in `field_aliases.json`.",
        "When a label is added there, the next crawl records that field at runtime.",
        "",
        "## Summary",
        "",
        f"- Excel properties: {counts.get('targets', 0)}",
        f"- Page labels seen: {counts.get('candidates', 0)}",
        f"- Already matched: {counts.get('matched', 0)}",
        f"- Suggested matches: {counts.get('suggested', 0)}",
        f"- Still unmatched: {counts.get('unmatched', 0)}",
        "",
        "## Excel properties",
        "",
        "| Column | Header | Program field | Colored |",
        "| --- | --- | --- | --- |",
    ]
    for row in payload.get("excel_targets") or []:
        lines.append(
            f"| {row.get('letter') or ''} | {row.get('header')} | {row.get('field') or '—'} | "
            f"{'yes' if row.get('colored') else 'no'} |"
        )
    lines.extend(["", "## Page labels", "", "| Status | Label | Field | Count | Sites | Sample values |", "| --- | --- | --- | --- | --- | --- |"])
    for item in payload.get("candidates") or []:
        hosts = ", ".join(sorted((item.get("hosts") or {}).keys()))
        samples = "; ".join(str(sample).replace("|", "/") for sample in (item.get("samples") or [])[:3])
        field = item.get("matched_field") or item.get("suggested_field") or "—"
        lines.append(
            f"| {item.get('status')} | {item.get('label')} | {field} | {item.get('count')} | {hosts} | {samples} |"
        )
    lines.extend(
        [
            "",
            "## How to extend the conversion table",
            "",
            "1. Review unmatched and suggested rows above.",
            "2. Add `\"label\": \"field_key\"` entries to `field_aliases.json`.",
            "3. Use the program field names from the Excel table (for example `language`, `pages`, `cover_type`).",
            "4. Restart or Search again — runtime matching uses the updated table.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
