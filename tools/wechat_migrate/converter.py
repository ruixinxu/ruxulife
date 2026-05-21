"""Convert a single WeChat article HTML page into a Jekyll post.

Pipeline:
  1. Parse HTML with BeautifulSoup, locate ``#js_content`` (article body).
  2. Normalise ``<img>`` tags: promote ``data-src`` → ``src``, drop noisy
     style/class attributes, drop WeChat-injected wrappers.
  3. Replace each ``src`` with the local Jekyll path returned by ``images.py``
     (caller passes in a URL → local-path map).
  4. ``markdownify`` the cleaned HTML to Markdown.
  5. Wrap the Markdown body in ``<div class="lang-zh" markdown="1"> ... </div>``
     to match the bilingual convention used in newer posts.
  6. Prepend YAML frontmatter matching the repo's existing post format.

The converter intentionally does not download images itself — that is the
caller's responsibility (``migrate.py build``) so failures can be tracked
in ``needs_review.json`` without coupling them to conversion.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Callable, Iterable

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

log = logging.getLogger(__name__)

# Asia/Singapore — matches the repo's ``timezone`` setting in _config.yml.
SGT = timezone(timedelta(hours=8))

# Tags that frequently appear in WeChat HTML but carry no semantic meaning
# once images and headings are extracted.
_STRIPPED_ATTRS = ("style", "class", "data-tools", "powered-by", "leaf",
                   "label", "data-darkmode-color", "data-darkmode-original-color",
                   "data-darkmode-bgcolor", "data-darkmode-original-bgcolor",
                   "data-darkmode-color-16725632",
                   "data-darkmode-original-color-16725632")

# Selectors that indicate "rich content" that cannot be rendered in Jekyll
# Markdown and should flag the article for manual review.
_RICH_CONTENT_SELECTORS = (
    ("mpvoice", "audio"),
    ("iframe.video_iframe", "video"),
    ("mp-common-mpaudio", "audio"),
    ("mp-miniprogram", "mini-program-card"),
    ("mp-common-product", "product-card"),
)


@dataclass
class ConvertedArticle:
    title: str
    body_markdown: str
    image_urls: list[str]          # original WeChat URLs, in order of appearance
    needs_review_reasons: list[str]


def _find_body(soup: BeautifulSoup) -> Tag:
    body = soup.find(id="js_content")
    if isinstance(body, Tag):
        return body
    # Some older WeChat exports use a different container.
    body = soup.find("div", class_="rich_media_content")
    if isinstance(body, Tag):
        return body
    raise RuntimeError("Could not find #js_content in WeChat article HTML")


# WeChat photo-album / 图片消息 posts ("share_content_page") embed their
# content in JavaScript variables rather than HTML elements. They look like:
#
#     picture_page_info_list: [
#       {
#         title: '三十八岁留影',
#         desc: 'Long body text with \x0a line breaks...',
#         cdn_url: 'https://mmbiz.qpic.cn/.../0?wx_fmt=jpeg',
#       },
#       { title: '', cdn_url: 'https://mmbiz.qpic.cn/.../0?wx_fmt=jpeg' },
#       ...
#     ]
#
# We reconstruct an HTML body from this by emitting the desc as paragraphs
# followed by each image in order.
_PHOTO_DESC_RE = re.compile(
    r"desc\s*:\s*'((?:[^'\\]|\\.)*)'",
    re.DOTALL,
)
_PHOTO_CDN_RE = re.compile(
    r"cdn_url\s*:\s*'(https?://mmbiz\.qpic\.cn/[^']+)'",
)


def _decode_js_string(s: str) -> str:
    """Decode a single-quoted JS string literal: \\x0a → newline, \\' → ', etc."""
    return (
        s.replace("\\x0a", "\n")
         .replace("\\x0d", "\r")
         .replace("\\n", "\n")
         .replace("\\t", "\t")
         .replace("\\'", "'")
         .replace('\\"', '"')
         .replace("\\\\", "\\")
    )


def _build_photo_album_html(html: str) -> str | None:
    """If ``html`` is a photo-album post, return synthetic HTML; else None."""
    descs = _PHOTO_DESC_RE.findall(html)
    cdns = _PHOTO_CDN_RE.findall(html)
    # Only consider this a photo-album page if we have at least one image and
    # one non-empty desc (or just images with a title fallback at the call site).
    if not cdns:
        return None
    # The first non-empty desc is the body; later descs are usually empty.
    body_text = ""
    for d in descs:
        decoded = _decode_js_string(d).strip()
        if decoded:
            body_text = decoded
            break

    parts: list[str] = ['<div id="js_content">']
    if body_text:
        for paragraph in body_text.split("\n"):
            paragraph = paragraph.strip()
            if paragraph:
                parts.append(f"<p>{paragraph}</p>")
    # Deduplicate image URLs while preserving order.
    seen: set[str] = set()
    for url in cdns:
        if url in seen:
            continue
        seen.add(url)
        parts.append(f'<p><img src="{url}" /></p>')
    parts.append("</div>")
    return "".join(parts)


def _promote_image_src(body: Tag) -> None:
    for img in body.find_all("img"):
        # Prefer data-src (lazy-load) over src (which may be a placeholder).
        data_src = img.get("data-src")
        if data_src:
            img["src"] = data_src
            del img["data-src"]


def _strip_noise(body: Tag) -> None:
    # Remove style/class/data-* attrs site-wide.
    for tag in body.find_all(True):
        for attr in _STRIPPED_ATTRS:
            if attr in tag.attrs:
                del tag.attrs[attr]
        for attr in list(tag.attrs):
            if attr.startswith("data-darkmode"):
                del tag.attrs[attr]


def _detect_rich_content(body: Tag) -> list[str]:
    reasons: list[str] = []
    for selector, label in _RICH_CONTENT_SELECTORS:
        if body.select_one(selector):
            reasons.append(f"contains {label}")
    return reasons


def _rewrite_image_urls(
    body: Tag,
    url_to_local: dict[str, str],
    fallback_marker: str = "WECHAT_IMG_MISSING",
) -> None:
    """Replace ``<img src>`` URLs with their local Jekyll paths.

    For URLs not present in the map (download failed), prepend a visible
    marker so the post review surfaces them clearly.
    """
    for img in body.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        local = url_to_local.get(src)
        if local:
            img["src"] = local
        else:
            img["alt"] = f"[{fallback_marker}] " + (img.get("alt") or "")


def _collect_image_urls(body: Tag) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for img in body.find_all("img"):
        src = img.get("src")
        if src and src.startswith(("http://", "https://")) and src not in seen:
            seen.add(src)
            urls.append(src)
    return urls


def parse_article(html: str) -> tuple[Tag, list[str], list[str]]:
    """Return ``(cleaned body tag, image urls, rich-content review reasons)``."""
    soup = BeautifulSoup(html, "lxml")
    body = _find_body(soup)
    _promote_image_src(body)
    rich = _detect_rich_content(body)
    _strip_noise(body)
    urls = _collect_image_urls(body)
    return body, urls, rich


def convert(
    html: str,
    title: str,
    url_to_local: dict[str, str] | None = None,
) -> ConvertedArticle:
    """Convert WeChat HTML to a Jekyll-ready Markdown body.

    ``url_to_local`` maps original image URLs to their final Jekyll-relative
    paths (e.g. ``/images/wechat/slug/0.jpg``). Pass ``None`` to leave
    original URLs in place (useful when callers want to enumerate images
    before downloading).
    """
    # First try the standard article path; if no #js_content, fall back to
    # the photo-album (share_content_page) format whose content lives in JS.
    try:
        soup = BeautifulSoup(html, "lxml")
        body = _find_body(soup)
    except RuntimeError:
        synthetic = _build_photo_album_html(html)
        if synthetic is None:
            raise
        soup = BeautifulSoup(synthetic, "lxml")
        body = _find_body(soup)
        log.info("Parsed %r as photo-album post (%d images)",
                 title, len(body.find_all("img")))
    _promote_image_src(body)
    rich = _detect_rich_content(body)
    _strip_noise(body)
    image_urls = _collect_image_urls(body)

    if url_to_local:
        _rewrite_image_urls(body, url_to_local)

    md_body = markdownify(
        str(body),
        heading_style="ATX",
        bullets="-",
        strip=["span"],
    )
    md_body = _tidy_markdown(md_body)
    return ConvertedArticle(
        title=title,
        body_markdown=md_body,
        image_urls=image_urls,
        needs_review_reasons=rich,
    )


_MULTI_BLANK = re.compile(r"\n{3,}")


def _tidy_markdown(md: str) -> str:
    # Collapse runs of blank lines and strip trailing whitespace per-line.
    lines = [line.rstrip() for line in md.splitlines()]
    text = "\n".join(lines).strip()
    return _MULTI_BLANK.sub("\n\n", text) + "\n"


def post_filename(create_time: int, slug: str) -> str:
    """Return ``YYYY-MM-DD-<slug>.markdown`` using Asia/Singapore tz."""
    dt = datetime.fromtimestamp(create_time, tz=SGT) if create_time else datetime.now(SGT)
    return f"{dt.strftime('%Y-%m-%d')}-{slug}.markdown"


def render_post(
    *,
    title: str,
    create_time: int,
    source_url: str,
    categories: Iterable[str],
    tags: Iterable[str],
    body_markdown: str,
) -> str:
    """Return the full ``---``-framed Jekyll post content (frontmatter + body).

    Frontmatter matches the conventions seen in the existing ``_posts/``:
    ``layout``, ``title``, ``date`` (with HH:MM:SS), ``categories``,
    ``tags``, ``comments``, ``share``. We add ``source_url`` for
    provenance — it isn't rendered by the existing layouts but is useful
    when reviewing imports.
    """
    dt = datetime.fromtimestamp(create_time, tz=SGT) if create_time else datetime.now(SGT)
    safe_title = title.replace('"', '\\"')
    cats = " ".join(categories)
    tag_list = ", ".join(tags)
    frontmatter = (
        "---\n"
        "layout: post\n"
        f'title:  "{safe_title}"\n'
        f"date:   {dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"categories: {cats}\n"
        f"tags: [{tag_list}]\n"
        f"source_url: {source_url}\n"
        "comments: true\n"
        "share: true\n"
        "---\n"
    )
    body = (
        '<div class="lang-zh" markdown="1">\n\n'
        f"{body_markdown.strip()}\n\n"
        "</div>\n"
    )
    return frontmatter + "\n" + body
