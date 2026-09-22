"""Multi-site manhwa/manga downloader entry point.

Structure follows the sibling scraper projects: config, term colors, site
modules under src/sites/, and a main() that asks for a URL and dispatches.
"""

from __future__ import annotations

import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from config import ROOT_DIR, classify_url, site_for_url
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


def choose_output_dir(url: str, site) -> Path:
    """Create a default output directory based on the URL slug."""
    from urllib.parse import parse_qs

    parsed = urlparse(url)
    slug = ""
    qs = parse_qs(parsed.query)
    slug = qs.get("toon", [""])[0]
    if not slug:
        # Path-based sites: /manga/<slug> or /manga/<slug>/chapter-1
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "manga":
            slug = parts[1]
    if not slug:
        slug = "unknown"
    safe_slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)
    return ROOT_DIR / "downloads" / site.key / safe_slug


def dispatch_download(
    url: str, site, out_dir: Path, chapters: list[int] | None = None
) -> Coroutine[Any, Any, dict[str, int]]:
    """Import the site module and return its (awaitable) download coroutine."""
    import importlib

    mod = importlib.import_module(site.module)
    if hasattr(mod, "download_series_url"):
        # New-style drivers take the raw URL and handle chapter selection.
        return mod.download_series_url(url, out_dir, chapters=chapters)
    return mod.download_series(url, out_dir, chapters=chapters)


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

    kind = classify_url(url, site)
    if kind == "chapter":
        cinfo("Chapter URL detected — only this chapter will be downloaded.")
        chapters = None
        out_dir = ROOT_DIR / "downloads" / site.key / "single_chapters"
    elif kind == "list":
        out_dir = choose_output_dir(url, site)
        cinfo(f"Series folder: {out_dir}")

        try:
            import asyncio
            import importlib

            async def _count_chapters():
                mod = importlib.import_module(site.module)
                async with mod.client() as c:
                    return await mod.fetch_all_chapter_links(c, url)

            links = asyncio.run(_count_chapters())
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
    if not cconfirm("Start download?", default=True):
        cinfo("Aborted.")
        pause()
        sys.exit(0)

    try:
        import asyncio

        stats = asyncio.run(dispatch_download(url, site, out_dir, chapters=chapters))
        csuccess(f"Done! {stats.get('chapters', 0)} chapter(s), {stats.get('images', 0)} image(s) downloaded.")
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
