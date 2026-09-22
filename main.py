"""Multi-site manhwa/manga downloader entry point.

Site drivers live in src/sites/ and self-register (see config.py). main.py is
completely site-agnostic: it asks for a URL, finds the driver, and delegates.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from config import CONVERT_TO_JPEG, DOWNLOADS_DIR, classify_url, site_for_url
from src.convert import convert_tree
from term import (
    cconfirm,
    cerror,
    cinfo,
    cinput,
    cprint,
    csuccess,
    cwarning,
    list_sites_table,
    pause,
    prompt_range,
)


def banner() -> str:
    return "Manhwa / Manga Downloader — multi-site async downloader"


def supported_sites() -> None:
    from config import SUPPORTED_SITES

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


def choose_output_dir(url: str, driver) -> Path:
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


async def _count_chapters(driver, url: str):
    return await driver.count_chapters(url)


def main() -> None:
    cprint(banner(), color="cyan", panel=True)
    supported_sites()

    raw = cinput("\nPaste a series list URL or chapter URL: ", color="green")
    url, err = resolve_input(raw)
    if err:
        cerror(err)
        pause()
        sys.exit(1)

    site = site_for_url(url)
    if site is None:
        cerror("Unsupported site. Supported domains are listed above.")
        pause()
        sys.exit(1)
    driver = site.driver

    kind = classify_url(url, site)
    if kind == "chapter":
        cinfo("Chapter URL detected — only this chapter will be downloaded.")
        chapters = None
        out_dir = DOWNLOADS_DIR / driver.key / driver.single_chapter_folder()
    elif kind == "list":
        out_dir = choose_output_dir(url, driver)
        cinfo(f"Series folder: {out_dir}")

        try:
            links = asyncio.run(_count_chapters(driver, url))
        except Exception as exc:
            cerror(f"Could not read chapter list: {exc}")
            pause()
            sys.exit(1)

        total = len(links)
        if total == 0:
            cerror("No chapters found on that list page.")
            pause()
            sys.exit(1)

        cinfo(f"Found {total} chapter(s).")
        chapters = prompt_range(total)
        if not chapters:
            cwarning("No chapters selected.")
            pause()
            sys.exit(0)
    else:
        cerror("Could not determine whether this is a list or chapter URL.")
        pause()
        sys.exit(1)

    cinfo(f"Output directory: {out_dir}")
    note_previous_incomplete(out_dir)
    if not cconfirm("Start download?", default=True):
        cinfo("Aborted.")
        pause()
        sys.exit(0)

    try:
        stats = asyncio.run(driver.download_series_url(url, out_dir, chapters=chapters))
        csuccess(f"Done! {stats.get('chapters', 0)} chapter(s), {stats.get('images', 0)} image(s) downloaded.")
        incomplete = stats.get("incomplete_chapters") or []
        if incomplete:
            cerror(f"{len(incomplete)} chapter(s) still incomplete after retries: {', '.join(incomplete)}")
            cwarning(
                "Recorded in incomplete_chapters.json under the output directory — "
                "re-running this same download will retry only what's missing."
            )
        if CONVERT_TO_JPEG:
            from config import JPEG_QUALITY

            convert_tree(out_dir, quality=JPEG_QUALITY)
    except httpx.HTTPStatusError as exc:
        cerror(f"HTTP error {exc.response.status_code}: {exc.request.url}")
    except httpx.HTTPError as exc:
        cerror(f"Network error: {exc}")
    except Exception as exc:
        cerror(f"Download failed: {exc}")
    finally:
        pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        cerror("\nAborted by user.")
        sys.exit(130)
