"""Bible book names, abbreviations, and scripture-reference regex parser.

Usage::

    from bible_books import find_scripture_refs

    refs = find_scripture_refs("See Romans 8:28 and also 1 Cor 15:29.")
    # [{"canonical": "Romans 8:28", "book": "Romans", ...}, ...]
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical book names → list of recognised abbreviations / alternate spellings
# Ordered: Old Testament (39) then New Testament (27)
# ---------------------------------------------------------------------------
BIBLE_BOOKS: dict[str, list[str]] = {
    # ── Old Testament ──────────────────────────────────────────────────────
    "Genesis": ["Gen", "Ge", "Gn"],
    "Exodus": ["Ex", "Exo", "Exod"],
    "Leviticus": ["Lev", "Le", "Lv"],
    "Numbers": ["Num", "Nu", "Nm", "Nb"],
    "Deuteronomy": ["Deut", "De", "Dt"],
    "Joshua": ["Josh", "Jos", "Jsh"],
    "Judges": ["Judg", "Jdg", "Jg", "Jdgs"],
    "Ruth": ["Rth", "Ru"],
    "1 Samuel": ["1Sam", "1 Sam", "1Sa", "1 Sa", "1Sm", "1 Sm"],
    "2 Samuel": ["2Sam", "2 Sam", "2Sa", "2 Sa", "2Sm", "2 Sm"],
    "1 Kings": ["1Kgs", "1 Kgs", "1Ki", "1 Ki", "1Kin", "1 Kin"],
    "2 Kings": ["2Kgs", "2 Kgs", "2Ki", "2 Ki", "2Kin", "2 Kin"],
    "1 Chronicles": ["1Chr", "1 Chr", "1Ch", "1 Ch", "1Chron", "1 Chron"],
    "2 Chronicles": ["2Chr", "2 Chr", "2Ch", "2 Ch", "2Chron", "2 Chron"],
    "Ezra": ["Ezr"],
    "Nehemiah": ["Neh", "Ne"],
    "Esther": ["Est", "Esth", "Es"],
    "Job": ["Jb"],
    "Psalms": ["Ps", "Psa", "Psm", "Pss", "Psalm"],
    "Proverbs": ["Prov", "Pro", "Prv", "Pr"],
    "Ecclesiastes": ["Eccl", "Ec", "Ecc", "Qoh"],
    "Song of Solomon": [
        "Song of Songs", "Song", "So", "SOS", "Cant", "Canticles", "SS",
    ],
    "Isaiah": ["Isa", "Is"],
    "Jeremiah": ["Jer", "Je", "Jr"],
    "Lamentations": ["Lam", "La"],
    "Ezekiel": ["Ezek", "Eze", "Ezk"],
    "Daniel": ["Dan", "Da", "Dn"],
    "Hosea": ["Hos", "Ho"],
    "Joel": ["Joe", "Jl"],
    "Amos": ["Am"],
    "Obadiah": ["Obad", "Ob"],
    "Jonah": ["Jon", "Jnh"],
    "Micah": ["Mic", "Mc"],
    "Nahum": ["Nah", "Na"],
    "Habakkuk": ["Hab"],
    "Zephaniah": ["Zeph", "Zep", "Zp"],
    "Haggai": ["Hag", "Hg"],
    "Zechariah": ["Zech", "Zec", "Zc"],
    "Malachi": ["Mal", "Ml"],
    # ── New Testament ──────────────────────────────────────────────────────
    "Matthew": ["Matt", "Mt"],
    "Mark": ["Mk", "Mr"],
    "Luke": ["Lk", "Lu"],
    "John": ["Jn", "Jhn"],
    "Acts": ["Ac"],
    "Romans": ["Rom", "Ro", "Rm"],
    "1 Corinthians": ["1Cor", "1 Cor", "1Co", "1 Co", "1Corinth", "1 Corinthians"],
    "2 Corinthians": ["2Cor", "2 Cor", "2Co", "2 Co", "2Corinth", "2 Corinthians"],
    "Galatians": ["Gal", "Ga"],
    "Ephesians": ["Eph", "Ep"],
    "Philippians": ["Phil", "Php", "Pp"],
    "Colossians": ["Col"],
    "1 Thessalonians": ["1Thess", "1 Thess", "1Th", "1 Th", "1Thes", "1 Thes"],
    "2 Thessalonians": ["2Thess", "2 Thess", "2Th", "2 Th", "2Thes", "2 Thes"],
    "1 Timothy": ["1Tim", "1 Tim", "1Ti", "1 Ti"],
    "2 Timothy": ["2Tim", "2 Tim", "2Ti", "2 Ti"],
    "Titus": ["Tit"],
    "Philemon": ["Phlm", "Phm"],
    "Hebrews": ["Heb"],
    "James": ["Jas", "Jm"],
    "1 Peter": ["1Pet", "1 Pet", "1Pe", "1 Pe", "1Pt", "1 Pt"],
    "2 Peter": ["2Pet", "2 Pet", "2Pe", "2 Pe", "2Pt", "2 Pt"],
    "1 John": ["1Jn", "1 Jn", "1Jhn", "1 Jhn"],
    "2 John": ["2Jn", "2 Jn", "2Jhn", "2 Jhn"],
    "3 John": ["3Jn", "3 Jn", "3Jhn", "3 Jhn"],
    "Jude": ["Jud", "Jd"],
    "Revelation": ["Rev", "Re", "Rv", "Apocalypse"],
}

# ---------------------------------------------------------------------------
# Build reverse lookup: any recognised name/abbreviation (lower) → canonical
# ---------------------------------------------------------------------------
_ABBREV_TO_CANONICAL: dict[str, str] = {}
for _canonical, _abbrevs in BIBLE_BOOKS.items():
    _ABBREV_TO_CANONICAL[_canonical.lower()] = _canonical
    for _abbrev in _abbrevs:
        _ABBREV_TO_CANONICAL[_abbrev.lower()] = _canonical

# ---------------------------------------------------------------------------
# Build the scripture-reference regex
# All book names + abbreviations sorted longest-first so the regex engine
# prefers longer matches (e.g. "Song of Songs" before "Song").
# ---------------------------------------------------------------------------
_ALL_NAMES: list[str] = sorted(
    list(BIBLE_BOOKS.keys())
    + [a for abbrevs in BIBLE_BOOKS.values() for a in abbrevs],
    key=len,
    reverse=True,
)

_BOOK_PATTERN = "|".join(re.escape(n) for n in _ALL_NAMES)

# Matches: BookName[.]  chapter:verse[-endverse]
# Examples: "Genesis 1:1", "1 Cor 15:29-30", "Ps. 23:1"
SCRIPTURE_RE = re.compile(
    r"\b(" + _BOOK_PATTERN + r")\.?\s+(\d{1,3}):(\d{1,3})(?:-(\d{1,3}))?",
    re.IGNORECASE,
)

# Also matches spoken form: "Genesis chapter 1 verse 1"
SCRIPTURE_SPOKEN_RE = re.compile(
    r"\b(" + _BOOK_PATTERN + r")\.?\s+chapter\s+(\d{1,3})\s+verse\s+(\d{1,3})",
    re.IGNORECASE,
)


def normalize_book(name: str) -> str | None:
    """Return the canonical book name for any recognised name or abbreviation."""
    return _ABBREV_TO_CANONICAL.get(name.lower().rstrip("."))


def find_scripture_refs(text: str) -> list[dict]:
    """Find all scripture references in *text*.

    Returns a list of dicts with keys:
        canonical  – normalised reference string, e.g. "Romans 8:28"
        book       – canonical book name
        chapter    – int
        verse      – int
        end_verse  – int or None
        match      – the matched substring
        start      – character offset of match start
        end        – character offset of match end
    """
    results: list[dict] = []

    def _add(m: re.Match, chapter_grp: int, verse_grp: int, end_verse_grp: int | None) -> None:
        book_str = m.group(1)
        canonical_book = normalize_book(book_str)
        if canonical_book is None:
            return
        chapter = int(m.group(chapter_grp))
        verse = int(m.group(verse_grp))
        end_verse = int(m.group(end_verse_grp)) if end_verse_grp and m.group(end_verse_grp) else None
        ref = f"{canonical_book} {chapter}:{verse}"
        if end_verse:
            ref += f"-{end_verse}"
        results.append(
            {
                "canonical": ref,
                "book": canonical_book,
                "chapter": chapter,
                "verse": verse,
                "end_verse": end_verse,
                "match": m.group(0),
                "start": m.start(),
                "end": m.end(),
            }
        )

    for m in SCRIPTURE_RE.finditer(text):
        _add(m, 2, 3, 4)

    for m in SCRIPTURE_SPOKEN_RE.finditer(text):
        _add(m, 2, 3, None)

    # Deduplicate by (canonical, start) keeping first occurrence
    seen: set[tuple[str, int]] = set()
    unique: list[dict] = []
    for r in sorted(results, key=lambda x: x["start"]):
        key = (r["canonical"], r["start"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return unique
