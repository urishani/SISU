import sys

sys.stdout.reconfigure(encoding="utf-8")
from book_crawler import BookCrawler, parse_html

c = BookCrawler(delay_seconds=0.1)
url = "https://ybook.co.il/products/3627539"
html, final = c.fetch(url)
soup = parse_html(html)

heading = soup.find("h2", string=lambda s: s and "פרטים נוספים" in s)
print("heading", heading)
block = heading.find_parent("div") if heading else None
# walk up a bit
node = heading
for i in range(6):
    if node is None:
        break
    print("LEVEL", i, node.name, node.get("class"), "len", len(str(node)))
    node = node.parent

# print the collapsible/accordion around פרטים נוספים
wrapper = heading.find_parent(class_=lambda c: c and ("accordion" in " ".join(c) or "collapsible" in " ".join(c) or "product" in " ".join(c))) if heading else None
print("wrapper", wrapper.name if wrapper else None, wrapper.get("class") if wrapper else None)

# find product details
for sel in [
    ".product__accordion",
    ".accordion",
    ".collapsible",
    ".product-details",
    "#ProductAccordion",
    ".rte",
    ".product__description",
]:
    found = soup.select(sel)
    print("SEL", sel, len(found))

# dump inner HTML of title-wrapper around פרטים נוספים
tw = soup.select_one(".title-wrapper-with-link")
if tw:
    parent = tw.parent
    print("TW PARENT", parent.name, parent.get("class"))
    print(parent.prettify()[:8000])
