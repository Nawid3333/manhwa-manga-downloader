# Manhwa and Manga Downloader

Multi-site async manhwa/manga downloader built on `httpx` (HTTP/2) + `lxml`,
in the style of the sibling scraper projects.

## Supported sites

| Key       | Site                  | Notes                                  |
| --------- | --------------------- | -------------------------------------- |
| `wfwf504` | wfwf504.com (늑대닷컴) | list pages, pagination, per-chapter folders |

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

It asks for a series list URL (or a single chapter URL), shows how many
chapters were found, then asks for a range (`1-10`, `5`, or `all`).

Direct CLI (single site script):

```pwsh
python download_wfwf_async.py https://wfwf504.com/view?toon=781&num=1 [out_dir]
python download_wfwf_async.py https://wfwf504.com/list?toon=781 --all [out_dir]
python download_wfwf_async.py https://wfwf504.com/list?toon=781 --range 1-203 [out_dir]
```

Legacy TUI:

```pwsh
python downloader_tui.py
```

## Adding a new site

1. Create `src/sites/<name>.py` exposing `client()`, `fetch_all_chapter_links()`,
   `download_series()` and the parse helpers.
2. Register it in `config.py` (`SUPPORTED_SITES`) with its domains and paths.
3. `main.py` picks it up automatically.

## Project layout

```
main.py                  interactive entry point (registry-driven)
config.py                Site registry + URL classification
term.py                  terminal colour helpers (rich with plain fallback)
src/htmlutil.py          shared lxml/XPath parsing helpers
src/sites/               one module per supported site
download_wfwf_async.py   standalone single-site script (legacy, still works)
downloader_tui.py        legacy rich TUI (kept for reference)
data/                    generated at runtime: downloads + logs (gitignored)
```

## Development

```pwsh
ruff check .
ruff format --check .
pyright
```

CI runs all three on every push/PR (see `.github/workflows/ci.yml`).