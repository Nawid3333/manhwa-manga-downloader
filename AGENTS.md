# AGENTS.md — Manhwa and Manga Downloader

Instructions for any AI coding agent or assistant working in this repository.

## What this is

A multi-site async manhwa/manga downloader (HTTP/2 via `httpx`, parsing via
`lxml`) built in the style of the sibling scraper projects (S.to, Aniworld,
BS.to). It downloads chapter images from supported sites and converts every
non-JPEG image to JPEG after each run.

## Architecture

- `main.py` — interactive entry point. **Completely site-agnostic**: it asks
  for a URL, asks `config.site_for_url()` which driver matches, and delegates.
- `config.py` — the site registry. Drivers **self-register**: any module in
  `src/sites/` that exports a module-level `driver` (a `SiteDriver` instance)
  is discovered automatically. Dropping a new file into `src/sites/` is
  enough to add a site; no central list to edit.
- `src/base.py` — `SiteDriver` abstract base class. New drivers subclass it
  and implement: `base_url`, `classify`, `list_chapters`, `image_urls`,
  `folder_name` (plus the URL-classification helpers). `matches()` on the
  base class compares the URL host against the driver's `domains`.
- `src/common.py` — shared HTTP client setup (limits, retries) and the
  generic downloader (`make_downloader`): concurrency-capped image fetching,
  `.part` atomic writes, resume logic with `_plausible_download` validation
  (0-byte/truncated leftovers are re-downloaded, not trusted).
- `src/convert.py` — post-download JPEG conversion. See below.
- `src/htmlutil.py` — lxml helpers (`attr`, text extraction).
- `term.py` — console output helpers (`cinfo`, `cwarning`, `cerror`, ...).
- `src/sites/mgread.py` — mgread.io driver (page-scraped listing, pagination).
- `src/sites/nelomanga.py` — nelomanga.net driver (JSON API + CDN URL pattern).
- `src/sites/wfwf.py` — wfwf504.com driver (list pages, pagination).

## Non-negotiables

- Windows 11 is the primary environment; code must also run on Linux
  (CI runs lint/type on `ubuntu-latest` and `windows-latest`).
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
2. Subclass `SiteDriver`, set `key`, `name`, `domains`.
3. Implement `classify`, `list_chapters`, `image_urls`, `folder_name`.
4. Export `driver = MyDriver()` at module level — discovery picks it up.
5. Update the README sites table.