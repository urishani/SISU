import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from book_crawler import Book, assign_listed_matches, books_match


def test_assign_matches_by_title() -> None:
    pending = [
        Book(url="", title="ילד אהוב", author="עפרה גלברט-אבני", publisher="מודן"),
        Book(url="", title="פרקי בנים", author="מחבר", publisher="מודן"),
    ]
    listed = [
        Book(url="https://modan.co.il/a", title="ילד אהוב", author="עפרה גלברט-אבני"),
        Book(url="https://modan.co.il/b", title="ספר אחר", author="מישהו"),
        Book(url="https://modan.co.il/c", title="פרקי בנים", author="מחבר"),
    ]
    pairs, leftover = assign_listed_matches(pending, listed)
    assert len(pairs) == 2
    assert leftover == []
    assert pairs[0][0].title == "ילד אהוב"
    assert pairs[0][1].url.endswith("/a")
    assert pairs[1][1].url.endswith("/c")


def test_assign_matches_by_isbn_once() -> None:
    wanted = Book(url="", title="One", isbn="9781234567897")
    listed = [
        Book(url="https://ex.example/p1", title="Different", isbn="9781234567897"),
        Book(url="https://ex.example/p2", title="Also", isbn="9781234567897"),
    ]
    pairs, leftover = assign_listed_matches([wanted], listed)
    assert len(pairs) == 1
    assert leftover == []
    assert pairs[0][1].url.endswith("/p1")


def test_untitled_listings_do_not_match() -> None:
    pending = [Book(url="", title="ילד אהוב", author="מחבר")]
    listed = [Book(url="https://ex.example/p1", title="")]
    pairs, leftover = assign_listed_matches(pending, listed)
    assert pairs == []
    assert leftover == pending
    assert not books_match(pending[0], listed[0])


if __name__ == "__main__":
    test_assign_matches_by_title()
    test_assign_matches_by_isbn_once()
    test_untitled_listings_do_not_match()
    print("ok")
