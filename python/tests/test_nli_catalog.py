import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nli_catalog import book_from_marc, build_sru_query, parse_catalog_url

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>00000nam a2200000 a 4500</leader>
  <controlfield tag="001">997014614179905171</controlfield>
  <datafield tag="020" ind1=" " ind2=" ">
    <subfield code="a">9789651234567</subfield>
  </datafield>
  <datafield tag="100" ind1="1" ind2=" ">
    <subfield code="a">הראל, עמוס</subfield>
    <subfield code="8">PreferredLanguageHeading</subfield>
  </datafield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">ספר בדיקה /</subfield>
    <subfield code="b">כותרת משנה</subfield>
  </datafield>
  <datafield tag="264" ind1=" " ind2="1">
    <subfield code="a">תל אביב :</subfield>
    <subfield code="b">כנרת,</subfield>
    <subfield code="c">[2026]</subfield>
  </datafield>
  <datafield tag="300" ind1=" " ind2=" ">
    <subfield code="a">1 מקור מקוון (430 עמודים)</subfield>
  </datafield>
</record>
"""


def test_parse_year_and_language_from_site_url() -> None:
    parsed = parse_catalog_url(
        "https://www.nli.org.il/he/search?projectName=NLI#&q=any,contains,ספרים"
        "&qInclude=facet_lang,exact,heb|,|facet_searchcreationdate,exact,[2026 TO 2026]&t=books"
    )
    assert parsed["language"] == "heb"
    assert parsed["year"] == "2026"
    assert parsed["material"] == "books"


def test_sru_query_is_hebrew_books_for_year() -> None:
    query = build_sru_query("https://www.nli.org.il/he/search?materialType=books", "2026")
    assert "alma.language=heb" in query
    assert "alma.mms_material_type=BK" in query
    assert "alma.main_pub_date=2026" in query
    assert "ספרים" not in query


def test_book_from_marc_fills_catalog_fields() -> None:
    record = ET.fromstring(SAMPLE)
    book = book_from_marc(record, year="2026")
    assert book is not None
    assert book.title.startswith("ספר בדיקה")
    assert "עמוס" in book.author
    assert "כנרת" in book.publisher
    assert book.year == "2026"
    assert book.pages == "430"
    assert book.isbn == "9789651234567"
    assert book.url.endswith("997014614179905171")


if __name__ == "__main__":
    test_parse_year_and_language_from_site_url()
    test_sru_query_is_hebrew_books_for_year()
    test_book_from_marc_fills_catalog_fields()
    print("ok")
