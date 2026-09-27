"""Extract unique Hebrew titles from SISU lists into cache/_he_titles.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NIQ = re.compile(r"[\u05B0-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]")
HE = re.compile(r"[\u0590-\u05FF]")
SOURCES = [
    ROOT / "lists" / "working.json",
    ROOT / "lists" / "stash.json",
    *(ROOT / "lists" / "named").glob("*.json"),
]


def main() -> None:
    seen: set[str] = set()
    titles: list[str] = []
    for path in SOURCES:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for book in data.get("books") or []:
            title = str(book.get("title") or book.get("title_he") or "").strip()
            if not title or not HE.search(title):
                continue
            key = NIQ.sub("", title)
            if key in seen:
                continue
            seen.add(key)
            titles.append(title)
    titles.sort()
    out = ROOT / "cache" / "_he_titles.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(titles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    chunk_dir = ROOT / "cache" / "nikud_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    size = 320
    for index in range(0, len(titles), size):
        chunk = titles[index : index + size]
        path = chunk_dir / f"in_{index // size:02d}.json"
        path.write_text(json.dumps(chunk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(titles)} titles, {(len(titles) + size - 1) // size} chunks")


if __name__ == "__main__":
    main()
