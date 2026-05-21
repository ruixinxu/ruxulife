"""Generate filesystem-safe slugs for Jekyll post filenames.

For Chinese titles, we transliterate to lowercase pinyin via ``pypinyin`` so
slugs remain readable (e.g. ``历史的教训`` → ``lishidejiaoxun``).
For ASCII titles, we lowercase and replace whitespace/punctuation with ``-``.
"""
from __future__ import annotations

import re
import unicodedata

from pypinyin import lazy_pinyin, Style

MAX_SLUG_LEN = 60
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x3400, 0x4DBF),    # CJK Extension A
    (0x20000, 0x2A6DF),  # CJK Extension B
    (0x3000, 0x303F),    # CJK Symbols and Punctuation
    (0xFF00, 0xFFEF),    # Halfwidth and Fullwidth Forms
)


def _has_cjk(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        for lo, hi in _CJK_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def slugify(title: str) -> str:
    """Return a lowercase ``[a-z0-9-]`` slug for ``title``, capped at 60 chars."""
    title = (title or "").strip()
    if not title:
        return "untitled"

    if _has_cjk(title):
        pieces = lazy_pinyin(title, style=Style.NORMAL, errors="ignore")
        slug = "-".join(p for p in pieces if p).lower()
    else:
        # NFKD strips accents (e.g. café → cafe) for cleaner URLs.
        nfkd = unicodedata.normalize("NFKD", title)
        slug = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()

    slug = _NON_SLUG_CHARS.sub("-", slug).strip("-")
    if not slug:
        slug = "untitled"
    if len(slug) > MAX_SLUG_LEN:
        slug = slug[:MAX_SLUG_LEN].rstrip("-")
    return slug


def disambiguate(slug: str, taken: set[str]) -> str:
    """Append ``-2``, ``-3``, … until ``slug`` is unique within ``taken``."""
    if slug not in taken:
        return slug
    n = 2
    while True:
        candidate = f"{slug}-{n}"
        if candidate not in taken:
            return candidate
        n += 1
