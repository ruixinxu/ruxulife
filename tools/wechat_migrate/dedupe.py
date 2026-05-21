"""Dedupe candidate WeChat articles against existing ``_posts/`` entries.

Match rule (confirmed with the user): title-only, normalised. We read every
``_posts/*.markdown`` file's frontmatter ``title`` and build a set of
normalised titles. Articles whose normalised title is in that set are
considered duplicates and skipped.

Normalisation:
  * NFKC unicode normalisation (full-width → half-width, compatibility forms)
  * Strip surrounding whitespace
  * Collapse internal whitespace
  * Lowercase ASCII characters (CJK is unchanged)
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

import frontmatter

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", title).strip()
    t = _WS.sub(" ", t)
    return t.lower()


def existing_titles(posts_dir: Path) -> set[str]:
    """Return the set of normalised titles already present in ``_posts/``."""
    titles: set[str] = set()
    if not posts_dir.is_dir():
        log.warning("Posts directory %s does not exist", posts_dir)
        return titles
    for path in sorted(posts_dir.glob("*.markdown")) + sorted(posts_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001 — best-effort scan
            log.warning("Failed to read frontmatter from %s: %s", path, exc)
            continue
        title = post.metadata.get("title") or ""
        norm = normalise_title(str(title))
        if norm:
            titles.add(norm)
    log.info("Indexed %d existing post titles from %s", len(titles), posts_dir)
    return titles


def partition(
    articles: list[dict],
    existing: set[str],
) -> tuple[list[dict], list[dict]]:
    """Split ``articles`` into ``(to_import, skipped)`` by normalised title."""
    to_import: list[dict] = []
    skipped: list[dict] = []
    for article in articles:
        norm = normalise_title(article.get("title", ""))
        if norm and norm in existing:
            skipped.append({**article, "_skipped_reason": "title-match"})
        else:
            to_import.append(article)
    return to_import, skipped
