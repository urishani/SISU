import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
from book_crawler import BookCrawler, parse_html, json_ld_objects

c = BookCrawler(delay_seconds=0.1)
url = "https://ybook.co.il/products/3627539"
html, final = c.fetch(url)
soup = parse_html(html)

print("=== json-ld offers ===")
for item in json_ld_objects(html):
    offers = item.get("offers")
    name = item.get("name")
    if offers or name:
        print("type", item.get("@type"), "name", name)
        print("offers", json.dumps(offers, ensure_ascii=False)[:800] if offers else None)

print("=== shopify product json ===")
for script in soup.find_all("script"):
    raw = script.string or ""
    if "compare_at_price" in raw or "compareAtPrice" in raw:
        print("script type", script.get("type"), "id", script.get("id"), "len", len(raw))
        # find compare_at snippets
        for m in re.finditer(r".{0,40}compare[_a-zA-Z]*[Pp]rice.{0,80}", raw):
            print(" ", m.group(0).replace("\n", " ")[:200])
            if m.start() > 2000:
                break

# product form
for tag in soup.select("[data-product-json], product-info, .product, [id*=Price]"):
    if tag.get("data-product-json"):
        print("data-product-json", tag.get("data-product-json")[:400])
print("price ids")
for tag in soup.select("[id*=price], [id*=Price], .price-item"):
    print(tag.name, tag.get("id"), tag.get("class"), tag.get("data-product-price"), tag.get_text(" ", strip=True)[:80])
