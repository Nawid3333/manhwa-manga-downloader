# Manhwa and Manga Downloader

Multi-site async manhwa/manga downloader built on `httpx` (HTTP/2) + `lxml`,
in the style of the sibling scraper projects. Downloads chapter images,
grouped per chapter, and converts every non-JPEG image to JPEG after each
run.

## Supported sites

| Key        | Site                       | Notes                                          |
| ---------- | -------------------------- | ---------------------------------------------- |
| `mgread`   | mgread.io                  | paginated series listing, reader scraping      |
| `nelomanga`| nelomanga.net (MangaNelo)  | JSON chapter API + CDN URL pattern             |
| `wfwf504`  | wfwf504.com (늑대닷컴)      | list pages, pagination, per-chapter folders    |
| `mangago`  | mangago.me                 | needs a logged-in session, see below           |

Drivers self-register: dropping a module into `src/sites/` that exports a
`driver` is enough to add a site. See `AGENTS.md`.

## Install

```pwsh
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Why mangago needs an account

mangago.me only shows a chapter's full page list in one request to a
logged-in session. Anonymous readers get one HTTP request per
panel instead of one per chapter — for a 50-page chapter that's ~50x more
requests just to enumerate the images, before any are even downloaded — and
anonymous traffic is more likely to hit mangago's Cloudflare bot challenge.

mangago's login form also sits behind an image captcha plus bot-management
that no plain HTTP client could satisfy, even with fully correct
credentials and a browser-matching request (verified the hard way) — so
logging in needs a real browser, not a request built by hand. Copy
`.env.example` to `.env`, fill in `MANGAGO_EMAIL` / `MANGAGO_PASSWORD`,
install the extra this needs once (`pip install -e ".[mangago]"` then
`playwright install firefox`), and run:

```pwsh
python -m tests.mangago_login
```

It runs a real headless Firefox instance, then prints a message and waits
once it's saved the captcha image to `data/mangago_captcha.png`. Open that
image, read the 5 characters, and write them to
`data/.mangago_captcha_answer.txt` (plain text, nothing else). The waiting
script picks that up, fills the field, submits, confirms the login, and
saves the session to `MANGAGO_COOKIE` in `.env` — that's what the driver
actually sends on every request afterward. Re-running the script first
checks whether that saved session is still valid and does nothing if so,
so this is only needed again once it actually expires.

The driver itself splits the work by what actually needs a browser and what
doesn't: the chapter list is plain HTML (ordinary httpx + lxml, same as the
other drivers), and the per-image CDN URLs turn out to be unsigned and
unauthenticated once known, so the actual downloads stay on the fast
httpx-based engine too. Only *finding* a chapter's image URLs needs a
browser — mangago encrypts them client-side (`var imgsrcs = '...'`, AES,
decrypted by an obfuscated bundled CryptoJS) and only exposes real `<img
src>` values once that JS actually runs, so the driver points a headless
Playwright page at the chapter and reads the DOM after decryption, rather
than reverse-engineering a cipher that could change at any time. Listing
pages have one more wrinkle: httpx's HTTP/2 stack gets a flat 403 from
mangago's Cloudflare bot management regardless of headers or cookie —
isolated by testing the identical request over HTTP/1.1, which works fine —
so the driver's client forces HTTP/1.1 rather than needing a browser there
too.

## Usage

Interactive entry point:

```pwsh
python main.py
```

It lists the supported sites, asks for a series list URL (or a single
chapter URL), shows how many chapters were found, then asks for a range
(`1-10`, `5`, or `all`). After the download, non-JPEG images under the
output tree are converted to JPEG (quality 90) on all CPU cores.

Output lands in `downloads/<site-key>/<series-slug>/<chapter-folder>/`.

Every run also writes a timestamped log file to `logs/` (e.g.
`logs/run_20260922_153000.log`) — the same messages shown on screen, plus
per-image request timing and the live adaptive concurrency limit (see
Reliability below) that aren't printed to the console. The path is printed
at startup.

## Reliability

A chapter is never reported as downloaded unless every one of its images is
actually present and passes a real integrity check, not just a status-code
check:

- Every downloaded file is verified with a full image decode (Pillow
  `Image.load()`, run off the event loop) before it's accepted — a dropped
  HTTP/2 stream or an overloaded CDN serving a truncated "200 OK" is caught
  and retried instead of being silently kept as a corrupt page. The same
  check gates resume: an existing file from a previous run is only trusted
  if it still decodes cleanly. Resume also recognizes a file that was
  already converted to `.jpg` by a previous run's conversion pass, even
  though the site's listing still points at the original (e.g. `.webp`)
  URL — otherwise every re-run of an already-converted series would
  silently redownload everything.
- A completed chapter's image count is recorded in
  `chapter_manifest.json` inside the output directory. On a later run, if a
  chapter's folder on disk still matches that count (verified with the same
  real decode check, not just a file count), its listing-page fetch is
  skipped entirely — a rerun over hundreds of already-downloaded chapters
  doesn't have to re-fetch each one's page just to confirm it has nothing
  to do.
- Each image gets its own retry budget (with jittered backoff; 429/503
  responses honor `Retry-After`), and on top of that, a chapter that still
  has missing or corrupt images after all of its images have had their
  individual attempts gets several more chapter-level retry rounds before
  it's given up on.
- Image concurrency is adaptive, not fixed: every failed attempt shrinks the
  allowed concurrency for the rest of the run (down to a floor), every
  success nudges it back up — the same idea as TCP's AIMD congestion
  control. A CDN that starts dropping streams or truncating responses under
  load gets backed off automatically instead of being hammered at a
  constant rate; `tests/benchmark_server.py` can be used to measure a given
  site's actual safe concurrency ahead of time.
- A chapter that's still incomplete after all of that is never silently
  accepted — it's reported by name at the end of the run and recorded in
  `incomplete_chapters.json` inside the output directory (merged across
  runs, cleared once a chapter actually completes). Re-running the same
  download later retries only what's missing; already-good files are left
  alone.

## Security

Every driver-supplied folder name (series slug, chapter folder) is routed
through the same sanitizer before it's ever joined onto a filesystem path,
at the two chokepoints that matter (`SiteDriver.series_folder` and the
download engine's chapter-folder join) rather than trusted per-driver. That
closes off path traversal via a crafted URL (e.g. a query parameter that
decodes to `../../..`) for every current and future site driver uniformly.

## Project layout

```
main.py                interactive entry point (registry-driven, site-agnostic)
config.py              site registry + conversion settings (self-discovery)
term.py                terminal colour helpers (rich with plain fallback)
src/base.py            SiteDriver abstract base class
src/common.py          shared client, generic downloader, resume validation
src/convert.py         post-download JPEG conversion (CPU, all cores)
src/htmlutil.py        shared lxml/XPath parsing helpers
src/sites/             one module per supported site (self-registering)
tests/                 benchmark + helper scripts (not part of the app)
downloads/, logs/      generated at runtime (gitignored)
```

## Format decision (JPEG)

The target format is always JPEG (quality 90); webp/png/gif/... sources are
re-encoded in place after a run and the sources deleted. The workload is
codec-bound, no usable GPU JPEG encoder exists, and measured CPU throughput
(~300 img/s on all cores, libjpeg-turbo via the bundled Pillow wheel) already
outruns network download. `optimize+progressive` was benchmarked and
rejected: identical pixel accuracy at ~40% slower encode. See the header of
`src/convert.py` and `tests/benchmark_convert.py`.

## Development

```pwsh
ruff check .
ruff format --check .
pyright
pytest
```

CI runs all four on every push/PR (see `.github/workflows/ci.yml`), and
again before an automated release is allowed to publish (see
`.github/workflows/auto-release.yml`). The test suite is fully hermetic —
every driver's HTTP calls go through `httpx.MockTransport`, never a live
site (see `tests/conftest.py`) — so it runs the same on a laptop or a CI
runner. `tests/benchmark_convert.py` and `tests/benchmark_server.py` are
separate, standalone scripts — the first benchmarks the conversion pipeline
on synthetic images, the second measures a real site's safe concurrency by
ramping request load against a real chapter's images
(`python -m tests.benchmark_server <chapter-or-series-url>`); neither is
part of the `pytest` run.

## License

GPL-3.0-or-later — see `LICENSE`.