import sys

sys.stdout.reconfigure(encoding="utf-8")
from book_crawler import BookCrawler, parse_html, labeled_value_pairs, extract_book_from_html
from field_map import collect_extra_pairs, attach_page_fields

c = BookCrawler(delay_seconds=0.1)
url = "https://ybook.co.il/products/3627539"
html, final = c.fetch(url)
soup = parse_html(html)

keys = ["מידע", "דנא", "ISBN", "מסת", "קטלוג", "פרטים", "מאפיינים", "מפרט", "נוסף"]
for t in soup.find_all(["h1", "h2", "h3", "h4", "summary", "button", "legend", "span", "div", "p", "b", "strong"]):
    text = t.get_text(" ", strip=True)
    if not text or len(text) > 80:
        continue
    if any(k in text for k in keys):
        print(t.name, t.get("class"), t.get("id"), repr(text))

print("=== isbn nearby ===")
for b in soup.find_all(["b", "strong", "span", "dt", "th"]):
    txt = b.get_text(" ", strip=True)
    if any(k in txt for k in ["ISBN", "דנא", "Danacode", "מסת"]):
        parent = b.parent
        print("TAG", b.name, repr(txt))
        print(" PARENT", parent.name, parent.get("class"), parent.get_text(" ", strip=True)[:500])
        print("---")

print("=== labeled pairs ===")
pairs = labeled_value_pairs(soup)
pairs.update(collect_extra_pairs(soup, html))
for k, v in pairs.items():
    print(repr(k), "=>", repr(v[:120]))

print("=== extracted book ===")
book = extract_book_from_html(html, final)
print("title", book.title)
print("price", book.price_ils)
print("isbn", book.isbn)
print("danacode", book.danacode)
print("pages", book.pages)
print("year", book.year)
print("cover", book.cover_type)
print("weight", book.weight_kg)
print("dims", book.height_cm, book.width_cm, book.thickness_cm)
print("page_fields", book.extra.get("page_fields"))
print("captured", book.captured_fields())
