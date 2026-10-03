"""Multi-site manhwa/manga downloader entry point.

Site drivers live in src/sites/ and self-register (see config.py). main.py is
completely site-agnostic: it takes a URL, finds the driver, and delegates.

Two front doors share one run: the interactive flow (no arguments: prompts
for the URL, the range and a confirmation, and pauses before exiting so a
double-clicked window stays readable) and a scriptable one (URL on the
command line, `--chapters`, `--out`, `--yes`, `--json`). In `--json` mode
every console message is routed to stderr so stdout carries exactly one
JSON object for the calling script, and the exit code says how the run
ended: 0 complete, 2 some chapters stayed incomplete, 1 error (a usage
error too, so 2 never means anything but "incomplete"), 130 aborted. The
object carries `"schema": RESULT_SCHEMA`; a caller refuses a schema it does
not know instead of misreading it (see README "Non-interactive").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx

from config import (
    CONVERT_TO_JPEG,
    DOWNLOADS_DIR,
    JPEG_QUALITY,
    LOGS_DIR,
    SUPPORTED_SITES,
    Site,
    classify_url,
    resolve_site,
)
from src.base import SiteDriver
from src.convert import convert_folders
from term import (
    cconfirm,
    cerror,
    cinfo,
    cinput,
    cprint,
    csuccess,
    cwarning,
    init_file_logging,
    list_sites_table,
    note_whole_chapters,
    parse_range,
    pause,
    prompt_range,
    set_console_stream,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INCOMPLETE = 2
EXIT_ABORTED = 130

# Version of the `--json` result object. Bump it when a field changes meaning
# or goes away; adding a field does not need a bump.
RESULT_SCHEMA = 1


def banner() -> str:
    return "Manhwa / Manga Downloader — multi-site async downloader"


def supported_sites() -> None:
    list_sites_table(SUPPORTED_SITES)


def resolve_input(raw: str) -> tuple[str, str | None]:
    """Return (normalized URL, error_message)."""
    url = raw.strip()
    if not url:
        return "", "No URL given."
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "", f"'{raw}' does not look like a valid URL."
    return url, None


def choose_output_dir(url: str, driver: SiteDriver) -> Path:
    """Default output directory: downloads/<site>/<series-slug>."""
    return DOWNLOADS_DIR / driver.key / driver.series_folder(url)


def note_previous_incomplete(out_dir: Path) -> None:
    """Heads-up if a prior run left chapters incomplete in this folder.

    download_series_url() retries these automatically just by being pointed
    at the same output directory again -- this just makes that visible
    instead of relying on the user remembering scrollback from days ago.
    """
    report = out_dir / "incomplete_chapters.json"
    if not report.exists():
        return
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        chapters = data.get("chapters", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return
    if chapters:
        names = ", ".join(c.get("folder", "?") for c in chapters)
        cwarning(f"{len(chapters)} chapter(s) from a previous run are still incomplete: {names}")
        cinfo("This run will retry them automatically.")


class _Parser(argparse.ArgumentParser):
    """argparse exits with 2 on a usage error, which is EXIT_INCOMPLETE here; exit with EXIT_ERROR instead."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface; with no URL the interactive flow runs instead."""
    parser = _Parser(prog="mangadl", description=banner())
    parser.add_argument("url", nargs="?", help="series list URL or chapter URL (omit for the interactive prompt)")
    parser.add_argument(
        "--chapters",
        metavar="RANGE",
        help="chapters to download: 'all', '5', '1-10' or '1,3,5-7' (default: prompt, or all with --yes)",
    )
    parser.add_argument("--out", metavar="DIR", type=Path, help="output directory (default: downloads/<site>/<series>)")
    parser.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    parser.add_argument(
        "--json", action="store_true", help="print one JSON result object to stdout, messages to stderr"
    )
    parser.add_argument(
        "--no-convert", action="store_true", help="keep downloaded images as-is (skip the post-run JPEG conversion)"
    )
    return parser


class RunError(Exception):
    """A run that cannot continue; the message is for the user."""


def _select_chapters(args: argparse.Namespace, links: list[tuple[str, float]], interactive: bool) -> list[int] | None:
    """Chapter numbers to download for a list URL (None = all).

    Selections are chapter numbers, bounded by the listing's lowest and
    highest one -- not 1..len(links), which would hide chapter 0 and every
    chapter above the count when a series starts late or has gaps.
    """
    numbers = [int(num) for _, num in links]
    first, last = min(numbers), max(numbers)
    if args.chapters is not None:
        text = args.chapters.strip().lower()
        if text in ("all", "*", ""):
            return None
        try:
            selected = parse_range(args.chapters, last, first)
        except ValueError as exc:
            raise RunError(str(exc)) from None
        if not selected:
            raise RunError(f"No chapters in {first}-{last} match {args.chapters!r}.")
        note_whole_chapters(args.chapters)
        return selected
    if args.yes and not interactive:
        return None
    return prompt_range(last, first)


def _series_name(driver: Any, url: str) -> str | None:
    """The series folder name the driver gives `url` (its `downloads/<site>/<series>` name), or None."""
    try:
        return driver.series_folder(url)
    except ValueError:  # e.g. a chapter URL that does not carry the series
        return None


def _result(
    site: Site | None,
    out_dir: Path | None,
    stats: dict[str, Any] | None,
    error: str | None,
    series: str | None = None,
) -> dict[str, Any]:
    stats = stats or {}
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "site": site.key if site else None,
        "series": series,
        "out_dir": str(out_dir) if out_dir else None,
        "chapters": int(stats.get("chapters", 0)),
        "images": int(stats.get("images", 0)),
        "failed_chapters": int(stats.get("failed_chapters", 0)),
        "complete_chapters": list(stats.get("complete_chapters") or []),
        "incomplete_chapters": list(stats.get("incomplete_chapters") or []),
    }
    if error:
        result["error"] = error
    return result


def run(args: argparse.Namespace, *, interactive: bool) -> tuple[int, dict[str, Any]]:
    """One download run; returns (exit code, result object) and never raises for user-facing errors."""
    site: Site | None = None
    out_dir: Path | None = None
    series: str | None = None
    try:
        raw = cinput("\nPaste a series list URL or chapter URL: ", color="green") if interactive else args.url
        url, err = resolve_input(raw)
        if err:
            raise RunError(err)

        site = asyncio.run(resolve_site(url))
        if site is None:
            raise RunError("Unsupported site: no driver matched the domain and the page could not be fetched.")
        driver = site.driver
        if site.key == "generic":
            cwarning("No dedicated driver for this site — using the best-effort generic scraper.")
        else:
            cinfo(f"Site: {site.name}")

        kind = classify_url(url, site)
        series = _series_name(driver, url)
        override: Path | None = Path(args.out) if args.out else None
        if kind == "chapter":
            cinfo("Chapter URL detected — only this chapter will be downloaded.")
            chapters = None
            links = None
            target = override or DOWNLOADS_DIR / driver.key / driver.single_chapter_folder()
        elif kind == "list":
            target = override or choose_output_dir(url, driver)
            cinfo(f"Series folder: {target}")
            try:
                links = asyncio.run(driver.count_chapters(url))
            except Exception as exc:
                raise RunError(f"Could not read chapter list: {exc}") from exc
            if not links:
                raise RunError("No chapters found on that list page.")
            cinfo(f"Found {len(links)} chapter(s).")
            chapters = _select_chapters(args, links, interactive)
        else:
            raise RunError("Could not determine whether this is a list or chapter URL.")
        out_dir = target

        cinfo(f"Output directory: {target}")
        note_previous_incomplete(target)
        # The count, so that one habitual Enter is not a surprise download of a whole series.
        planned = 1 if links is None else len(driver.select_chapters(links, chapters))
        if not args.yes and not cconfirm(f"Start download of {planned} chapter(s)?", default=True):
            cinfo("Aborted.")
            return EXIT_OK, _result(site, out_dir, None, None, series)

        # The listing fetched for the prompt above is reused, not fetched again.
        stats = asyncio.run(driver.download_series_url(url, target, chapters=chapters, links=links))
        csuccess(f"Done! {stats.get('chapters', 0)} chapter(s), {stats.get('images', 0)} image(s) downloaded.")
        incomplete = stats.get("incomplete_chapters") or []
        if incomplete:
            cerror(f"{len(incomplete)} chapter(s) still incomplete after retries: {', '.join(incomplete)}")
            cwarning(
                "Recorded in incomplete_chapters.json under the output directory — "
                "re-running this same download will retry only what's missing."
            )
        if CONVERT_TO_JPEG and not args.no_convert:
            # Only the chapter folders this run wrote: `--out` may be a folder
            # that already holds other images, which must not be re-encoded.
            written = [*(stats.get("complete_chapters") or []), *incomplete]
            convert_folders([target / folder for folder in written], quality=JPEG_QUALITY)
        bookkeeping_error = stats.get("bookkeeping_error")
        if bookkeeping_error:
            # incomplete_chapters.json may now be stale, and OmniScan's importer
            # trusts it: this run must not read as a success.
            message = (
                f"Downloaded, but the bookkeeping files could not be updated ({bookkeeping_error}). "
                "Fix that (on Windows: close whatever has them open) and run the same download again."
            )
            cerror(message)
            return EXIT_ERROR, _result(site, out_dir, stats, message, series)
        code = EXIT_INCOMPLETE if incomplete or stats.get("failed_chapters") else EXIT_OK
        return code, _result(site, out_dir, stats, None, series)
    except RunError as exc:
        cerror(str(exc))
        return EXIT_ERROR, _result(site, out_dir, None, str(exc), series)
    except httpx.HTTPStatusError as exc:
        message = f"HTTP error {exc.response.status_code}: {exc.request.url}"
        cerror(message)
        return EXIT_ERROR, _result(site, out_dir, None, message, series)
    except httpx.HTTPError as exc:
        message = f"Network error: {exc}"
        cerror(message)
        return EXIT_ERROR, _result(site, out_dir, None, message, series)
    except Exception as exc:
        message = f"Download failed: {exc}"
        cerror(message)
        return EXIT_ERROR, _result(site, out_dir, None, message, series)
    except KeyboardInterrupt:
        message = "Aborted by user."
        cerror(message)
        return EXIT_ABORTED, _result(site, out_dir, None, message, series)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.json:
        set_console_stream(sys.stderr)
    interactive = args.url is None

    cprint(banner(), color="cyan", panel=True)
    try:
        log_path = init_file_logging(LOGS_DIR)
    except OSError as exc:  # e.g. an installed copy whose logs/ is not writable: run without a log file
        cwarning(f"No log file for this run: {exc}")
    else:
        cinfo(f"Logging to {log_path}")
    if interactive:
        supported_sites()

    try:
        code, result = run(args, interactive=interactive)
    finally:
        if interactive:
            pause()
    if args.json:
        print(json.dumps(result))
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        cerror("\nAborted by user.")
        sys.exit(130)
