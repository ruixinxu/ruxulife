"""WeChat MP admin API client.

Two responsibilities:
  1. ``list_articles`` — paginate ``cgi-bin/appmsg`` to enumerate every
     published article on the configured 公众号.
  2. ``fetch_article_html`` — download a single ``mp.weixin.qq.com/s/...``
     article HTML page (wired up in a later todo).

All calls are paced with ``config.request_delay_seconds`` and retry with
exponential backoff on transient errors. The list endpoint is the authority
on titles, publish timestamps, and cover images.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, asdict
from typing import Iterator

import requests

from config import Config

log = logging.getLogger(__name__)

APPMSG_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"
PAGE_SIZE = 20
MAX_RETRIES = 5
ARTICLE_TIMEOUT = 60

# WeChat ``base_resp.ret`` codes we recognise.
RET_OK = 0
RET_FREQ_LIMIT = 200013
RET_INVALID_TOKEN = 200002
RET_INVALID_SESSION = 200003


class WeChatAuthError(RuntimeError):
    """Raised when the token/cookie is rejected by WeChat. Re-capture and re-run."""


class WeChatRateLimitError(RuntimeError):
    """Raised when WeChat returns a frequency-control error. Wait and retry later."""


@dataclass(frozen=True)
class ArticleSummary:
    """One row from the appmsg list endpoint.

    Field names match the JSON returned by WeChat so the cache is auditable.
    """
    aid: str
    appmsgid: int
    title: str
    link: str
    create_time: int  # unix epoch seconds
    update_time: int
    digest: str
    cover: str

    @staticmethod
    def from_json(item: dict) -> "ArticleSummary":
        return ArticleSummary(
            aid=str(item.get("aid", "")),
            appmsgid=int(item.get("appmsgid", 0)),
            title=(item.get("title") or "").strip(),
            link=item.get("link", ""),
            create_time=int(item.get("create_time", 0)),
            update_time=int(item.get("update_time", 0)),
            digest=item.get("digest", ""),
            cover=item.get("cover", ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _build_session(cfg: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": cfg.user_agent,
        "Referer": "https://mp.weixin.qq.com/",
        "Cookie": cfg.cookie,
        "X-Requested-With": "XMLHttpRequest",
    })
    return session


def _check_ret(payload: dict) -> None:
    base = payload.get("base_resp") or {}
    ret = base.get("ret", 0)
    if ret == RET_OK:
        return
    msg = base.get("err_msg", "")
    if ret in (RET_INVALID_TOKEN, RET_INVALID_SESSION):
        raise WeChatAuthError(
            f"WeChat rejected our auth (ret={ret}, err_msg={msg!r}). "
            f"Your token or cookie has expired — re-capture and re-run. "
            f"See README.md 'Capturing the cookie and token'."
        )
    if ret == RET_FREQ_LIMIT:
        raise WeChatRateLimitError(
            f"WeChat frequency limit hit (ret={ret}, err_msg={msg!r}). "
            f"Wait ~1 hour and re-run; the tool will resume from cache."
        )
    raise RuntimeError(f"WeChat appmsg error ret={ret} err_msg={msg!r}")


def _request_page(
    session: requests.Session,
    cfg: Config,
    begin: int,
    count: int,
) -> dict:
    params = {
        "action": "list_ex",
        "begin": begin,
        "count": count,
        "fakeid": cfg.fakeid,
        "type": 9,  # 9 = published articles
        "query": "",
        "token": cfg.token,
        "lang": "zh_CN",
        "f": "json",
        "ajax": 1,
    }

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(APPMSG_URL, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            _check_ret(payload)
            return payload
        except (WeChatAuthError, WeChatRateLimitError):
            # Non-retryable — surface immediately so the user can act.
            raise
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            backoff = min(60, (2 ** attempt) + random.uniform(0, 1))
            log.warning(
                "appmsg request failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt, MAX_RETRIES, exc, backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(
        f"appmsg request failed after {MAX_RETRIES} attempts: {last_exc!r}"
    )


def list_articles(cfg: Config, limit: int | None = None) -> Iterator[ArticleSummary]:
    """Yield every published article on the configured 公众号, newest first.

    Pages through ``cgi-bin/appmsg?action=list_ex`` with ``PAGE_SIZE`` rows per
    request, sleeping ``cfg.request_delay_seconds`` between pages. Stops when
    the endpoint returns an empty page or when ``limit`` summaries have been
    yielded.
    """
    session = _build_session(cfg)
    begin = 0
    yielded = 0
    total_known: int | None = None

    while True:
        log.info("Fetching appmsg page begin=%d count=%d", begin, PAGE_SIZE)
        payload = _request_page(session, cfg, begin=begin, count=PAGE_SIZE)

        if total_known is None:
            total_known = int(payload.get("app_msg_cnt", 0))
            log.info("WeChat reports %d total published articles", total_known)

        items = payload.get("app_msg_list") or []
        if not items:
            log.info("Empty page at begin=%d — listing complete", begin)
            return

        for item in items:
            yield ArticleSummary.from_json(item)
            yielded += 1
            if limit is not None and yielded >= limit:
                log.info("Reached --limit %d, stopping listing", limit)
                return

        begin += len(items)
        if total_known is not None and begin >= total_known:
            log.info("Reached app_msg_cnt=%d — listing complete", total_known)
            return

        time.sleep(cfg.request_delay_seconds)


def fetch_article_html(cfg: Config, url: str) -> str:
    """Download a single ``mp.weixin.qq.com/s/...`` article HTML page.

    The article HTML endpoint does not require the admin session cookie — it
    is publicly readable — but we send the same User-Agent + Referer for
    consistency with the listing requests. We do *not* send the admin cookie
    here to reduce the risk of triggering admin-side rate limits.
    """
    headers = {
        "User-Agent": cfg.user_agent,
        "Referer": "https://mp.weixin.qq.com/",
    }
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=ARTICLE_TIMEOUT)
            if resp.status_code == 200:
                # WeChat sometimes serves Chinese pages without a charset; the
                # body declares UTF-8 via a meta tag. Force UTF-8 to avoid
                # mojibake from requests's ISO-8859-1 fallback.
                resp.encoding = "utf-8"
                return resp.text
            if resp.status_code in (404, 410):
                raise RuntimeError(
                    f"Article not found (HTTP {resp.status_code}): {url}"
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            backoff = min(60, (2 ** attempt) + random.uniform(0, 1))
            log.warning(
                "Article fetch failed (attempt %d/%d) for %s: %s — retry in %.1fs",
                attempt, MAX_RETRIES, url, exc, backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(
        f"Article fetch failed after {MAX_RETRIES} attempts for {url}: {last_exc!r}"
    )
