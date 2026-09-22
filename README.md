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

Drivers self-register: dropping a module into `src/sites/` that exports a
`driver` is enough to add a site. See `AGENTS.md`.

## Install

```pwsh
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

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

## Reliability

A chapter is never reported as downloaded unless every one of its images is
actually present and passes a real integrity check, not just a status-code
check:

- Every downloaded file is verified with a full image decode (Pillow
  `Image.load()`, run off the event loop) before it's accepted — a dropped
  HTTP/2 stream or an overloaded CDN serving a truncated "200 OK" is caught
  and retried instead of being silently kept as a corrupt page. The same
  check gates resume: an existing file from a previous run is only trusted
  if it still decodes cleanly.
- Each image gets its own retry budget (with backoff; 429/503 responses
  honor `Retry-After`), and on top of that, a chapter that still has
  missing or corrupt images after all of its images have had their
  individual attempts gets several more chapter-level retry rounds before
  it's given up on.
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
runner. `tests/benchmark_convert.py` is a separate, standalone script that
benchmarks the conversion pipeline on synthetic images; it isn't part of
the `pytest` run.

## License

GPL-3.0-or-later — see `LICENSE`.