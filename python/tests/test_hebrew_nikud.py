import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hebrew_nikud import menukad_title, phonetic_from_nikud, strip_nikud
from hebrew_text import hebrew_phonetic


def test_menukad_known_words() -> None:
    assert strip_nikud(menukad_title("ילד אהוב")) == "ילד אהוב"
    assert "ֶ" in menukad_title("ילד") or "ֵ" in menukad_title("ילד")
    assert phonetic_from_nikud(menukad_title("ילד אהוב")) == "Yeled Ahuv"
    assert phonetic_from_nikud(menukad_title("של")) == "Shel"
    assert phonetic_from_nikud(menukad_title("ספר")) == "Sefer"
    assert phonetic_from_nikud(menukad_title("נער")) == "Na'ar"
    assert phonetic_from_nikud(menukad_title("פעם")) == "Pa'am"
    assert phonetic_from_nikud(menukad_title("ישראל")) == "Yisra'el"
    assert phonetic_from_nikud(menukad_title("המדריך")) == "Hamadrikh"
    assert phonetic_from_nikud(menukad_title("פרקי בנים")) == "Pirkei Banim"
    assert phonetic_from_nikud(menukad_title("חרבות ברזל")) == "Charvot Barzel"


def test_hebrew_phonetic_uses_nikud() -> None:
    assert hebrew_phonetic("ילד אהוב") == "Yeled Ahuv"
    assert hebrew_phonetic("נער") == "Na'ar"
    assert hebrew_phonetic("1984") == ""


if __name__ == "__main__":
    test_menukad_known_words()
    test_hebrew_phonetic_uses_nikud()
    print("ok")
