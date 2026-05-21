"""Download images embedded in WeChat articles to the Jekyll ``images/`` tree.

WeChat hosts images on ``mmbiz.qpic.cn`` and enforces a ``Referer`` check:
requests without a ``mp.weixin.qq.com`` referer get HTTP 403. We always send
the referer header.

Images in the article body use ``data-src`` (lazy load) more often than
``src``, so the converter should prefer ``data-src``.
"""
from __future__ import annotations

import logging
import mimetypes
import random
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 4
TIMEOUT = 60

# WeChat appends ``?wx_fmt=jpeg`` etc.; map to file extensions.
_FMT_TO_EXT = {
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "bmp": ".bmp",
}


def _ext_from_response(resp: requests.Response, url: str) -> str:
    # 1) wx_fmt query param is the most reliable signal.
    lower_url = url.lower()
    for fmt, ext in _FMT_TO_EXT.items():
        if f"wx_fmt={fmt}" in lower_url:
            return ext
    # 2) Content-Type header.
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if content_type:
        guess = mimetypes.guess_extension(content_type)
        if guess:
            return ".jpg" if guess == ".jpe" else guess
    # 3) Fall back to .jpg (WeChat's most common format).
    return ".jpg"


def download_image(
    url: str,
    out_dir: Path,
    index: int,
    user_agent: str,
) -> Path:
    """Download a single image into ``out_dir`` as ``<index>.<ext>``.

    Returns the local path written. Raises ``RuntimeError`` on persistent
    failure (caller is expected to log and continue, flagging the article
    in ``needs_review.json``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://mp.weixin.qq.com/",
    }

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True)
            if resp.status_code == 200:
                ext = _ext_from_response(resp, url)
                path = out_dir / f"{index}{ext}"
                with path.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
                return path
            if resp.status_code in (403, 404, 410):
                # Hotlink protection or deleted image — non-retryable.
                raise RuntimeError(
                    f"Image unavailable (HTTP {resp.status_code}): {url}"
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            backoff = min(30, (2 ** attempt) + random.uniform(0, 1))
            log.warning(
                "Image download failed (attempt %d/%d) for %s: %s — retry in %.1fs",
                attempt, MAX_RETRIES, url, exc, backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(
        f"Image download failed after {MAX_RETRIES} attempts for {url}: {last_exc!r}"
    )
