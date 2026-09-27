import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hebrew_text import hebrew_phonetic


def test_known_titles() -> None:
    assert hebrew_phonetic("ילד אהוב") == "Yeled Ahuv"
    assert hebrew_phonetic("פרקי בנים") == "Pirkei Banim"
    assert hebrew_phonetic("של") == "Shel"
    assert hebrew_phonetic("המדריך") == "Hamadrikh"
    assert hebrew_phonetic("אהבה") == "Ahava"
    assert hebrew_phonetic("ישראל") == "Yisra'el"
    assert hebrew_phonetic("החיים") == "Hachayim"
    assert hebrew_phonetic("ספר") == "Sefer"
    assert hebrew_phonetic("1984") == ""
    assert hebrew_phonetic("חרבות ברזל") == "Charvot Barzel"
    assert hebrew_phonetic("100 סיפורים ואנקדוטות למוטיבציה והשראה").startswith("100 Sipurim")


def test_keeps_latin_words() -> None:
    spelled = hebrew_phonetic("MRI - המדריך המלא")
    assert spelled.startswith("MRI")
    assert "Hamadrikh" in spelled


if __name__ == "__main__":
    test_known_titles()
    test_keeps_latin_words()
    from book_crawler import Book

    book = Book(url="https://example.com/book", title="ילד אהוב", title_phonetic="Yld Ahov")
    book.extra["phonetic_source"] = "algorithm"
    assert not book.ensure_phonetic()
    assert book.title_phonetic == "Yld Ahov"
    assert book.ensure_phonetic(replace=True)
    assert book.title_phonetic == "Yeled Ahuv"
    print("ok")
