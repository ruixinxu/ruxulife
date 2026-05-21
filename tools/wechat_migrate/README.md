# WeChat → Jekyll migration tool

One-time bulk migration of articles from a WeChat 公众号 (public account) into
this Jekyll repo's `_posts/` directory.

This tool is intentionally **kept out of the Jekyll build** (`_config.yml`
excludes the `tools/` directory) and is run manually on your laptop.

## What it does

1. Calls the WeChat MP admin `cgi-bin/appmsg` endpoint to enumerate every
   published article on the configured account.
2. Skips articles whose normalised title already exists in `_posts/`.
3. Downloads each remaining article's HTML page and parses out
   `#js_content`.
4. Downloads embedded images from `mmbiz.qpic.cn` (with the required
   `Referer` header) into `images/wechat/<slug>/`.
5. Converts the cleaned HTML to Markdown, wraps it in the bilingual
   `<div class="lang-zh" markdown="1">` block this repo uses, prepends
   Jekyll frontmatter that matches the existing posts, and writes
   `_posts/YYYY-MM-DD-<slug>.markdown`.

Slugs for Chinese titles are transliterated to lowercase pinyin
(`历史的教训` → `li-shi-de-jiao-xun`); date prefixes use Asia/Singapore
to match `_config.yml`.

## Architecture

| File | Responsibility |
|---|---|
| `migrate.py` | CLI orchestrator: `list` → `plan` → `fetch` → `build` |
| `wechat_api.py` | `appmsg` listing + single-article HTML fetch, with retries and rate-limit detection |
| `config.py` | Loads & validates `config.json` |
| `dedupe.py` | NFKC-normalised title matching against existing `_posts/` |
| `slug.py` | Pinyin/ASCII slugifier + collision disambiguator |
| `images.py` | Image downloader with WeChat `Referer` header |
| `converter.py` | HTML → Markdown + frontmatter + bilingual wrapper |
| `cache/` | Per-run state (gitignored): `articles.json`, `articles_html/`, `todo.json`, `skipped.json`, `state.json`, `needs_review.json` |

The pipeline is **idempotent and resumable**: every stage caches its output,
so a token expiry mid-run or a partial network failure is recovered by
re-running the same command.

## Prerequisites

- Python 3.10+
- You are an **admin** of the WeChat 公众号 (so you can log into
  `https://mp.weixin.qq.com`).

## Setup

```powershell
cd tools\wechat_migrate
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Then edit `config.json` and paste in your `token` + `cookie` (see next section).

## Capturing the cookie and token

WeChat's admin platform uses QR-code login and has no public API, so the
cleanest flow is **manual cookie paste** from your browser DevTools.

1. Open Chrome/Edge, log into `https://mp.weixin.qq.com`.
2. Once logged in, open DevTools (F12) → **Network** tab.
3. In the sidebar, click **内容与互动 → 草稿箱** (or **已发表**).
4. In the Network panel, look for a request to
   `cgi-bin/appmsg?action=list_ex&...`.
5. Click the request:
   - From the **request URL**, copy the value of the `token=` query
     parameter → paste into `config.json` `token`.
   - From **Request Headers**, copy the entire `Cookie:` header value (one
     long string starting with something like `ua_id=...; pgv_pvid=...; ...`)
     → paste into `config.json` `cookie`.
6. Save `config.json`.

⚠️ Tokens expire roughly every **2 hours**. If a run fails with an
expired-token error, re-capture and re-run — the tool is resumable and will
skip work it has already completed.

## Usage

Recommended: run the migration on a dedicated git branch so the resulting
commit can be reviewed as a PR.

```powershell
git checkout -b wechat-import

# 1. List every published article into cache/articles.json
python migrate.py list

# 2. Compare against existing _posts/ and write cache/todo.json
python migrate.py plan

# 3. Fetch HTML for every article in todo.json (cached on disk)
python migrate.py fetch

# 4. Download images, convert to markdown, write to ../../_posts/
python migrate.py build

# Or do everything end-to-end:
python migrate.py all
```

### Useful flags

| Flag | Effect |
|---|---|
| `--dry-run` | Show what would happen without writing `_posts/` or `images/` |
| `--limit 3` | Cap to first 3 articles (great for a first run) |
| `--refresh` | Force re-fetch (re-list / re-download HTML / re-build) |
| `--config PATH` | Use a config file other than `./config.json` |

### Recommended first run

```powershell
python migrate.py list                 # confirm credentials work
python migrate.py plan --dry-run       # see how many are duplicates
python migrate.py all --limit 3        # process 3 articles end-to-end
# spot-check the 3 generated posts with `bundle exec jekyll serve`
python migrate.py all                  # process everything
```

## Output layout

- `_posts/YYYY-MM-DD-<slug>.markdown` — generated Jekyll posts
- `images/wechat/<slug>/<n>.<ext>` — downloaded article images
- `tools/wechat_migrate/cache/articles.json` — full inventory
- `tools/wechat_migrate/cache/todo.json` — articles still to import
- `tools/wechat_migrate/cache/skipped.json` — articles skipped as duplicates
- `tools/wechat_migrate/cache/state.json` — per-article build progress (for resume)
- `tools/wechat_migrate/cache/needs_review.json` — articles that contain
  content that can't be cleanly rendered (video, audio, mini-program cards,
  image download failures) — review these manually after the run.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ret: 200002` from appmsg | Token expired or invalid | Re-capture token + cookie, re-run |
| `ret: 200013` from appmsg | Rate limited (frequency control) | Wait ~1 hour, re-run; tool will resume |
| `403` downloading an image | Hotlink protection (Referer missing) | Already handled — if it persists, the image URL has been deleted on WeChat's side; entry is logged to `needs_review.json` |
| Slug collisions | Two articles share a title | Tool auto-appends `-2`, `-3`, etc. to filename |
| Post body shows raw HTML | markdown engine couldn't parse content | Check the `<div class="lang-zh" markdown="1">` wrapper is intact (the `markdown="1"` attribute is required for kramdown to render markdown inside the block) |
| Images show `[WECHAT_IMG_MISSING]` in alt text | Download failed for that URL | Check `needs_review.json` for the article entry; the original URL is preserved there |

## After the run

1. `git status` — review the generated posts and downloaded images.
2. `bundle exec jekyll serve` — spot-check rendering locally at <http://localhost:4000>.
3. Inspect `cache/needs_review.json` and fix flagged posts manually.
4. Inspect `cache/skipped.json` to make sure no real duplicates were missed
   (titles with different punctuation will NOT match, e.g. `"Be Happy, if
   Not, Be Crazy"` vs `"Be Happy if Not Be Crazy"` — review and merge by hand).
5. Commit on the `wechat-import` branch, open a PR.

## Re-using this tool later

This is built as a **one-time** migration tool, but the modules are
deliberately decoupled: `wechat_api.py` + `converter.py` are reusable from
a GitHub Action if you ever decide to switch to ongoing sync. The
`appmsg` calls would just need to be paged for articles newer than the
latest `source_url` already in `_posts/`.

