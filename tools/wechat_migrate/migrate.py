"""CLI entry point for the WeChat → Jekyll migration tool.

Subcommands:
    list   — paginate the WeChat appmsg endpoint and dump cache/articles.json
    plan   — dedupe against existing _posts/ and write cache/todo.json
    fetch  — download HTML for every article in todo.json (cached)
    build  — convert cached HTML to Jekyll posts + download images
    all    — run list → plan → fetch → build in sequence

Global flags:
    --config PATH    path to config.json (default: ./config.json)
    --dry-run        do not write _posts/ or images/, just log
    --limit N        cap the number of articles processed (debug)
    --refresh        force re-listing even if articles.json cache exists
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from config import Config
from converter import convert, post_filename, render_post
from dedupe import existing_titles, partition
from images import download_image
from slug import disambiguate, slugify
from wechat_api import (
    ArticleSummary,
    WeChatAuthError,
    WeChatRateLimitError,
    fetch_article_html,
    list_articles,
)

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
ARTICLES_JSON = CACHE_DIR / "articles.json"
TODO_JSON = CACHE_DIR / "todo.json"
SKIPPED_JSON = CACHE_DIR / "skipped.json"
HTML_CACHE_DIR = CACHE_DIR / "articles_html"
STATE_JSON = CACHE_DIR / "state.json"
NEEDS_REVIEW_JSON = CACHE_DIR / "needs_review.json"

REPO_ROOT = HERE.parent.parent
POSTS_DIR = REPO_ROOT / "_posts"
IMAGES_DIR = REPO_ROOT / "images" / "wechat"


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.json",
        help="Path to config.json (default: ./config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing _posts/ or images/.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of articles processed (useful for testing).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-fetch of articles.json even if cached.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate",
        description="One-time migration of WeChat 公众号 articles into Jekyll.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("list", "Paginate appmsg endpoint and dump cache/articles.json"),
        ("plan", "Dedupe against existing _posts/ and write cache/todo.json"),
        ("fetch", "Download HTML for every article in todo.json"),
        ("build", "Convert cached HTML to Jekyll posts + download images"),
        ("all", "Run list → plan → fetch → build in sequence"),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_common_flags(p)
    return parser


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_cached_articles() -> list[dict] | None:
    if not ARTICLES_JSON.exists():
        return None
    try:
        return json.loads(ARTICLES_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("Corrupt %s — ignoring and re-fetching", ARTICLES_JSON)
        return None


def _save_articles(articles: list[ArticleSummary]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = [a.to_dict() for a in articles]
    ARTICLES_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logging.info("Wrote %d articles to %s", len(payload), ARTICLES_JSON)


def cmd_list(args: argparse.Namespace) -> int:
    cached = None if args.refresh else _load_cached_articles()
    if cached is not None:
        logging.info(
            "Using cached %s (%d articles). Pass --refresh to re-fetch.",
            ARTICLES_JSON, len(cached),
        )
        return 0

    cfg = Config.load(args.config)
    try:
        articles = list(list_articles(cfg, limit=args.limit))
    except WeChatAuthError as exc:
        logging.error("%s", exc)
        return 2
    except WeChatRateLimitError as exc:
        logging.error("%s", exc)
        return 3

    if not articles:
        logging.warning("WeChat returned no articles — nothing to cache.")
        return 1

    if args.dry_run:
        logging.info("--dry-run: would write %d articles to %s",
                     len(articles), ARTICLES_JSON)
        for a in articles[:5]:
            logging.info("  %s | %s", a.title, a.link)
        return 0

    _save_articles(articles)
    return 0


COMMANDS = {
    "list": cmd_list,
    "plan": lambda args: cmd_plan(args),
    "fetch": lambda args: cmd_fetch(args),
    "build": lambda args: cmd_build(args),
    "all": lambda args: cmd_all(args),
}


def _require_articles_cache() -> list[dict]:
    cached = _load_cached_articles()
    if cached is None:
        raise SystemExit(
            f"No cached article list found at {ARTICLES_JSON}. "
            f"Run `python migrate.py list` first."
        )
    return cached


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def cmd_plan(args: argparse.Namespace) -> int:
    articles = _require_articles_cache()
    existing = existing_titles(POSTS_DIR)
    to_import, skipped = partition(articles, existing)

    if args.limit is not None:
        to_import = to_import[: args.limit]

    logging.info(
        "Plan: %d to import, %d skipped (title-match against %d existing posts)",
        len(to_import), len(skipped), len(existing),
    )

    if args.dry_run:
        for a in to_import[:10]:
            logging.info("  IMPORT: %s", a.get("title"))
        for a in skipped[:10]:
            logging.info("  SKIP:   %s", a.get("title"))
        return 0

    _write_json(TODO_JSON, to_import)
    _write_json(SKIPPED_JSON, skipped)
    logging.info("Wrote %s and %s", TODO_JSON, SKIPPED_JSON)
    return 0


def _safe_html_filename(article: dict) -> str:
    # ``aid`` is the most stable per-article identifier; fall back to appmsgid.
    aid = article.get("aid") or str(article.get("appmsgid") or "unknown")
    # ``aid`` looks like ``2247483647_1``; slashes are unlikely but sanitise anyway.
    return aid.replace("/", "_") + ".html"


def cmd_fetch(args: argparse.Namespace) -> int:
    if not TODO_JSON.exists():
        raise SystemExit(
            f"No plan found at {TODO_JSON}. Run `python migrate.py plan` first."
        )
    todo: list[dict] = json.loads(TODO_JSON.read_text(encoding="utf-8"))
    if args.limit is not None:
        todo = todo[: args.limit]

    cfg = Config.load(args.config)
    HTML_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    fetched = 0
    cached = 0
    failed: list[dict] = []
    for i, article in enumerate(todo, start=1):
        url = article.get("link")
        if not url:
            logging.warning("[%d/%d] Skipping article with no link: %s",
                            i, len(todo), article.get("title"))
            continue
        out_path = HTML_CACHE_DIR / _safe_html_filename(article)
        if out_path.exists() and not args.refresh:
            cached += 1
            continue
        logging.info("[%d/%d] Fetching %s", i, len(todo), article.get("title"))
        if args.dry_run:
            continue
        try:
            html = fetch_article_html(cfg, url)
            out_path.write_text(html, encoding="utf-8")
            fetched += 1
        except Exception as exc:  # noqa: BLE001
            logging.error("Failed to fetch %s: %s", url, exc)
            failed.append({"title": article.get("title"), "link": url,
                           "error": str(exc)})
        import time as _t
        _t.sleep(cfg.request_delay_seconds)

    logging.info("Fetch complete: %d new, %d cached, %d failed",
                 fetched, cached, len(failed))
    if failed:
        fail_path = CACHE_DIR / "fetch_failures.json"
        _write_json(fail_path, failed)
        logging.warning("Recorded %d failures to %s", len(failed), fail_path)
    return 0


def _load_state() -> dict:
    if not STATE_JSON.exists():
        return {"built": {}}
    try:
        return json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"built": {}}


def _save_state(state: dict) -> None:
    _write_json(STATE_JSON, state)


def _existing_post_filenames(posts_dir: Path) -> set[str]:
    return {p.stem for p in posts_dir.glob("*.markdown")} | {p.stem for p in posts_dir.glob("*.md")}


def cmd_build(args: argparse.Namespace) -> int:
    if not TODO_JSON.exists():
        raise SystemExit(
            f"No plan found at {TODO_JSON}. Run `python migrate.py plan` first."
        )
    todo: list[dict] = json.loads(TODO_JSON.read_text(encoding="utf-8"))
    if args.limit is not None:
        todo = todo[: args.limit]
    if not todo:
        logging.info("Nothing to build.")
        return 0

    if args.dry_run:
        # Dry-run skips network calls (image downloads), so we don't need
        # real cookies. Use defaults to avoid forcing the user to populate
        # config.json just for a dry-run preview.
        cfg = Config(
            token="dry-run", cookie="dry-run", fakeid="",
            user_agent="Mozilla/5.0 (dry-run)",
            request_delay_seconds=0,
            default_categories=["wechat"],
            default_tags=["wechat"],
        )
    else:
        cfg = Config.load(args.config)
    state = _load_state()
    needs_review: list[dict] = []
    taken_stems = _existing_post_filenames(POSTS_DIR)
    written = 0
    skipped = 0

    for i, article in enumerate(todo, start=1):
        aid = article.get("aid") or str(article.get("appmsgid") or f"idx{i}")
        if state["built"].get(aid) and not args.refresh:
            skipped += 1
            continue
        title = article.get("title") or "Untitled"
        link = article.get("link") or ""
        create_time = int(article.get("create_time") or 0)

        html_path = HTML_CACHE_DIR / (aid.replace("/", "_") + ".html")
        if not html_path.exists():
            logging.warning("[%d/%d] No cached HTML for %s (aid=%s) — run fetch first",
                            i, len(todo), title, aid)
            needs_review.append({"aid": aid, "title": title, "link": link,
                                 "reason": "no cached HTML"})
            continue

        html = html_path.read_text(encoding="utf-8")
        try:
            # Pass 1: enumerate image URLs and rich-content warnings.
            preview = convert(html, title=title)
        except Exception as exc:  # noqa: BLE001
            logging.error("[%d/%d] Failed to parse %s: %s", i, len(todo), title, exc)
            needs_review.append({"aid": aid, "title": title, "link": link,
                                 "reason": f"parse error: {exc}"})
            continue

        slug = slugify(title)
        # Disambiguate filename by stem (date+slug) against existing _posts/.
        from datetime import datetime as _dt
        from converter import SGT as _SGT
        date_prefix = _dt.fromtimestamp(create_time, tz=_SGT).strftime("%Y-%m-%d") \
            if create_time else _dt.now(_SGT).strftime("%Y-%m-%d")
        base_stem = f"{date_prefix}-{slug}"
        final_stem = disambiguate(base_stem, taken_stems)
        # Recover the slug portion (everything after the date prefix) for
        # the image directory.
        final_slug = final_stem[len(date_prefix) + 1:]
        taken_stems.add(final_stem)

        # Download images.
        url_to_local: dict[str, str] = {}
        image_failures: list[str] = []
        img_dir = IMAGES_DIR / final_slug
        for idx, url in enumerate(preview.image_urls):
            if args.dry_run:
                url_to_local[url] = f"/images/wechat/{final_slug}/{idx}.jpg"
                continue
            try:
                local_path = download_image(url, img_dir, idx, cfg.user_agent)
                rel = local_path.relative_to(REPO_ROOT).as_posix()
                url_to_local[url] = "/" + rel
            except Exception as exc:  # noqa: BLE001
                logging.warning("Image download failed for %s: %s", url, exc)
                image_failures.append(url)

        # Pass 2: rewrite image src and emit markdown.
        final = convert(html, title=title, url_to_local=url_to_local)

        post_path = POSTS_DIR / (final_stem + ".markdown")
        rendered = render_post(
            title=title,
            create_time=create_time,
            source_url=link,
            categories=cfg.default_categories,
            tags=cfg.default_tags,
            body_markdown=final.body_markdown,
        )

        if args.dry_run:
            logging.info("[%d/%d] DRY-RUN would write %s (%d images)",
                         i, len(todo), post_path.name, len(preview.image_urls))
        else:
            post_path.parent.mkdir(parents=True, exist_ok=True)
            post_path.write_text(rendered, encoding="utf-8")
            state["built"][aid] = {"path": str(post_path.relative_to(REPO_ROOT)),
                                   "title": title}
            _save_state(state)
            written += 1
            logging.info("[%d/%d] Wrote %s", i, len(todo), post_path.name)

        if image_failures or final.needs_review_reasons:
            needs_review.append({
                "aid": aid, "title": title, "link": link,
                "post_path": str(post_path.relative_to(REPO_ROOT)),
                "image_failures": image_failures,
                "content_reasons": final.needs_review_reasons,
            })

    logging.info("Build complete: %d written, %d already built (skipped), %d need review",
                 written, skipped, len(needs_review))
    if needs_review:
        _write_json(NEEDS_REVIEW_JSON, needs_review)
        logging.warning("Recorded %d entries needing manual review to %s",
                        len(needs_review), NEEDS_REVIEW_JSON)
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    for step in (cmd_list, cmd_plan, cmd_fetch, cmd_build):
        rc = step(args)
        if rc != 0:
            return rc
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:
        logging.error("Subcommand %r is not implemented yet.", args.command)
        return 64
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
