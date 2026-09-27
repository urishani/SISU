---
name: hebrew-phonetics
description: >-
  Convert Hebrew names and book titles to phonetic English spelling (transliteration, not translation).
  Use when filling Title (phonetics), romanizing Hebrew, adding lexicon entries, or the user mentions
  phonetics, transliteration, Heblish, or תעתיק.
---

# Hebrew phonetics

SISU stores a **phonetic English spelling**, not a translation. `של` is `Shel`, never `Of`.

The runtime model does **not** call an LLM:

- `python/hebrew_nikud.py` — menukad (niqqud) titles, then English phonetics from those vowels
- `python/hebrew_phonetic_model.py` — lexicon lookup, Ha-/Be-/Le- prefixes, letter fallback
- `python/hebrew_phonetic_lexicon.json` — catalog words learned from scan titles
- Call `hebrew_phonetic(text)` / `hebrew_menukad(text)` from `hebrew_text`

Menukad is stored as `title_he_nikud`. Phonetics use apostrophes to split vowel hits (`Na'ar`, `Pa'am`, `Yisra'el`).

## Spelling conventions

- Title Case per Hebrew token (`Yeled Ahuv`)
- `ח` = Ch, `כ`/`ק` = K, `ך` = kh, `צ` = Ts, `ש` = Sh
- `ו` as a vowel is usually `o` (lexicon may use `u`, e.g. `Ahuv`)
- `י` at the start of a word is `Y`, otherwise often `i`
- Keep already-Latin words (`MRI`, `Asterix`, `Minecraft`)
- Construct forms stay construct: `פרקי` → `Pirkei`, `שפת` → `Sfat`

## When a spelling is wrong

1. Add the exact Hebrew token to `python/hebrew_phonetic_lexicon.json` under `words`
2. Prefer a stem that prefixes can reuse (`מדריך` → `Madrikh` so `המדריך` becomes `Hamadrikh`)
3. From the `python` folder, run `python tests/test_hebrew_phonetic.py`

Do not translate meaning. Do not replace a `phonetic_source` of `llm` or `manual`.
