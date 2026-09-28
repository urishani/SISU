import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_config import _normalize, parse_settings_pack, publisher_is_preferred
from publisher_sites import publisher_name_matches_filter, resolve_publisher_site, sort_publisher_rows


def test_filter_matches_text_anywhere_in_the_name() -> None:
    assert publisher_name_matches_filter("כנרת זמורה", "זמורה")
    assert publisher_name_matches_filter("ידיעות ספרים", "עות")
    assert not publisher_name_matches_filter("ידיעות ספרים", "מודן")
    assert publisher_name_matches_filter("Modan", "mod")
    assert publisher_name_matches_filter("", "מודן")
    assert publisher_name_matches_filter("מודן", "  ")


def test_sort_publisher_and_website_keeps_blanks_last() -> None:
    rows = [
        ["ב", "https://b.example"],
        ["", "https://blank.example"],
        ["א", ""],
        ["ג", "https://a.example"],
    ]
    sort_publisher_rows(rows, "name", False)
    assert [row[0] for row in rows] == ["א", "ב", "ג", ""]
    sort_publisher_rows(rows, "name", True)
    assert [row[0] for row in rows] == ["ג", "ב", "א", ""]
    sort_publisher_rows(rows, "url", False)
    assert [row[1] for row in rows] == [
        "https://a.example",
        "https://b.example",
        "https://blank.example",
        "",
    ]


def test_preferred_flag_defaults_to_true_and_longest_name_wins() -> None:
    assert publisher_is_preferred("מודן", {})
    flags = {"עם": False, "עם עובד": True}
    assert publisher_is_preferred("עם", flags) is False
    assert publisher_is_preferred("עם עובד", flags) is True
    assert publisher_is_preferred("ספר בלי אתר", flags) is True


def test_unpreferred_site_is_skipped_unless_deep_search(monkeypatch) -> None:
    monkeypatch.setattr(
        "app_config.configured_publisher_site",
        lambda publisher, mapping=None: "https://www.modan.co.il/",
    )
    monkeypatch.setattr("app_config.publisher_is_preferred", lambda publisher, flags=None: False)
    assert resolve_publisher_site("מודן") is None
    assert resolve_publisher_site("מודן", include_unpreferred=True) == "https://www.modan.co.il/"


def test_settings_keep_preferred_flag() -> None:
    data = _normalize(
        {
            "publishers": {"מודן": "https://www.modan.co.il/"},
            "publisher_preferred": {"מודן": False},
        }
    )
    assert data["publishers"]["מודן"] == "https://www.modan.co.il/"
    assert data["publisher_preferred"]["מודן"] is False
    pack = parse_settings_pack(
        {
            "publishers": {"מודן": {"url": "https://www.modan.co.il/", "preferred": False}},
        }
    )
    assert pack is not None
    assert pack["publishers"]["מודן"] == "https://www.modan.co.il/"
    assert pack["publisher_preferred"]["מודן"] is False
