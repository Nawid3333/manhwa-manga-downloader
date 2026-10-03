# AGENTS.md — Manhwa and Manga Downloader

Instructions for any AI coding agent or assistant working in this repository.

## What this is

A multi-site async manhwa/manga downloader (HTTP/2 via `httpx`, parsing via
`lxml`) built in the style of the sibling scraper projects (S.to, Aniworld,
BS.to). It downloads chapter images from supported sites and converts every
non-JPEG image to JPEG after each run.

## Architecture

- `main.py` — entry point, **completely site-agnostic**: it takes a URL
  (prompted, or from the command line), asks `config.resolve_site()` which
  driver handles it, and delegates. Two front doors share one `run()`: the
  interactive flow (no arguments) and the scriptable CLI
  (`main.py URL [--chapters RANGE] [--out DIR] [--yes] [--json]
  [--no-convert]`). `--json` routes every console message to stderr
  (`term.set_console_stream`) so stdout carries exactly one result object;
  exit codes are 0 / 2 (chapters stayed incomplete) / 1 (error, including
  usage errors) / 130 (interrupted). Nothing may write to stdout before
  that object, including at import time (`config._registry_warning` uses
  stderr). Chapter selections are parsed by the pure `term.parse_range()`
  for both doors.
- `config.py` — the site registry and URL resolution. Drivers
  **self-register**: any module in `src/sites/` that exports a module-level
  `driver` (a `SiteDriver` instance) is discovered automatically. Dropping a
  new file into `src/sites/` is enough to add a site; no central list to
  edit. `site_for_url()` matches by domain only (no network);
  `resolve_site()` does that first and, when nothing matches, fetches the
  page once with the plain client and offers it to every driver's `sniff()`
  in descending `priority` — the first claimant wins. It also picks
  `DATA_DIR`, home of `downloads/`, `logs/` and `.env`: the source tree
  when running from one (`is_checkout`), else the current working
  directory, so an installed copy never writes into `site-packages`.
- `src/base.py` — `SiteDriver` abstract base class. New drivers subclass it
  and implement: `base_url`, `classify`, `list_chapters`, `image_urls`,
  `folder_name` (plus the URL-classification helpers). `matches()` on the
  base class compares the URL host against the driver's `domains`. A driver
  for a *family* of sites leaves `domains` empty and instead overrides
  `sniff(url, html)` (claim a page by its content) and sets `priority`
  (higher wins; specific markup above 0, the catch-all far below), plus
  `referer_for(url)` so requests carry the right origin without a fixed
  host. The engine passes `referer_for(url)` into `client()` for every run.
  `main.run()` hands the listing it fetched for the range prompt to
  `download_series_url(..., links=...)`, so a run reads a series' listing
  once; a driver that overrides `download_series_url` must forward `links`.
- `src/common.py` — shared HTTP client setup (limits, retries) and the
  generic downloader (`make_downloader`): concurrency-capped image fetching,
  `.part` atomic writes, resume logic with `_plausible_download` validation
  (0-byte/truncated leftovers are re-downloaded, not trusted). A page is
  saved under the extension of its decoded format (`_suffix_for`), never
  trusted from its URL.
- `src/convert.py` — post-download JPEG conversion. See below.
- `src/htmlutil.py` — lxml helpers (`attr`, text extraction).
- `term.py` — console output helpers (`cinfo`, `cwarning`, `cerror`, ...).
- `src/sites/mgread.py` — mgread.io driver (page-scraped listing, pagination).
- `src/sites/nelomanga.py` — nelomanga.net driver (JSON API + CDN URL pattern).
- `src/sites/wfwf.py` — wfwf504.com driver (list pages, pagination).
- `src/sites/mangago.py` — mangago.me driver (logged-in session required;
  headless Playwright reads chapter image URLs the site encrypts
  client-side, see README.md "Why mangago needs an account"). Playwright
  (the optional `mangago` extra) is imported only when a browser is
  needed, so the driver registers without it and `client()` says how to
  install it.
- `src/sites/mangadex.py` — mangadex.org driver over the official API
  (feed paging by `total`, `MANGADEX_LANGS`, MangaDex@Home image nodes,
  descriptive User-Agent, 429 backoff). Chapter numbers are remembered per
  chapter id because chapter URLs only carry a UUID.
- `src/sites/madara.py` — content-sniffed driver (`priority = 10`, no
  domains) for every site built on the Madara WordPress theme: three-step
  chapter listing (inline → `ajax/chapters/` → `admin-ajax.php`), lazy-load
  aware reader scraping.
- `src/sites/generic.py` — best-effort fallback (`priority = -100`,
  `sniff()` claims any HTML). Pure heuristics: chapter-number patterns in
  URLs, same-host chapter links (preferring those under the series path),
  and "largest group of images wins" (shared container in the DOM or one
  JSON array in a script) after dropping obvious non-page images. It is
  the last resort, never the first: any domain match or Madara sniff
  outranks it.
- `tests/mangago_login.py` — standalone (non-pytest) login helper for the
  mangago driver: drives a real headless Firefox through mangago's
  captcha-gated login and saves the resulting session to `.env`.

## Non-negotiables

- Windows 11 is the primary environment; code must also run on Linux
  (CI runs lint, format, type check and tests on `windows-latest` and
  `ubuntu-latest`, on Python 3.11 and 3.13, and builds the package).
- Python floor is 3.11 (`requires-python`); CI tests the floor version, so
  never use a newer-only API without raising the floor deliberately.
- The target image format is always JPEG (quality 90). Non-JPEG downloads
  are converted after a run on the CPU (all cores). **Do not add a GPU
  backend** — this was evaluated and rejected with benchmarks: the workload
  is codec-bound (decode + libjpeg encode), no usable GPU JPEG encoder
  exists, and measured CPU throughput (~300 img/s on all cores) already
  outruns network download. Details in the header of `src/convert.py`.
- Never trust an existing file during resume without checking it
  (`_plausible_download`): a 0-byte or magic-byte-less file must be
  re-downloaded.
- The output layout is a contract with OmniScan's importer
  (`src/omniscan/importer/downloader.py` there): chapter folders named
  `num<N>_<title>` (`num0_`/`numunknown_` only when the number is unknown),
  `.part` files while an image is in flight, and `chapter_manifest.json` /
  `incomplete_chapters.json` in the series folder. The `--json` result
  object is part of that contract too (README "The `--json` result"):
  adding a field is fine, but changing or removing one means bumping
  `main.RESULT_SCHEMA`. Changing any of these needs the matching change in
  OmniScan (see README "Translating with OmniScan").
- Never commit downloads, logs, scratch probes (`_*.py`), or secrets.
  `.gitignore` covers `downloads/`, `logs/`, `data/`, `series_*/`.
- Definition of done for a change: `ruff check .`, `ruff format --check .`,
  `pyright` all clean (same gate as CI).

## Conventions

- Conventional Commits (`feat:`, `fix:`, `chore:`). Version is bumped
  automatically by python-semantic-release on push to main — do not bump
  `version` in `pyproject.toml` by hand.
- Ruff format with LF line endings (`.gitattributes` normalizes; ruff is
  pinned to `line-ending = "lf"` so Windows and CI agree).
- Type hints everywhere; pyright `basic` mode gates CI.
- README must reflect the actual supported-sites table and usage; when a
  driver is added or changed, update README in the same change.

## Adding a site

1. Copy the shape of an existing driver in `src/sites/`.
2. Subclass `SiteDriver`, set `key`, `name`, `domains` (or, for a family of
   sites recognizable from their markup, leave `domains` empty and
   implement `sniff()` with a `priority` above the generic driver's).
3. Implement `classify`, `list_chapters`, `image_urls`, `folder_name`.
4. Export `driver = MyDriver()` at module level — discovery picks it up.
5. Update the README sites table.
6. Tests stay hermetic (`httpx.MockTransport` via the `mock_client`
   fixture); never hit a live site from the suite. The one live check is
   `.github/workflows/live-smoke.yml`, a weekly download of one MangaDex
   chapter that opens an issue when it breaks; it is not part of `pytest`.
