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
```

CI runs all three on every push/PR (see `.github/workflows/ci.yml`). There
is no live-site test suite; `tests/benchmark_convert.py` benchmarks the
conversion pipeline on synthetic images.

## License

GPL-3.0-or-later — see `LICENSE`.