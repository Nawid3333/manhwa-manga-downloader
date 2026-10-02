"""SiteDriver: the base class every site driver inherits from.

Adding a new site = one small class with only the site-specific parts:

    class MySite(SiteDriver):
        key = "mysite"
        name = "My Site"
        domains = ("mysite.com",)
        referer = "https://mysite.com/"

        def classify(self, url): ...            # 'list' | 'chapter' | 'unknown'
        async def list_chapters(self, client, url): ...   # [(url, num), ...]
        async def image_urls(self, client, chapter_url): ...  # ordered urls
        def folder_name(self, chapter_url): ...  # output folder for a chapter

The engine (concurrency, retries, .part writes, resume, stats) and the
facade consumed by main.py are inherited — no plumbing needed.

Drivers are normally picked by domain (`matches`). A driver that serves a
*family* of sites rather than one host (the Madara WordPress theme, the
best-effort generic scraper) has no domains to match; it claims a URL from
the fetched page instead (`sniff`), and `priority` decides which claimant
wins when several would -- see config.resolve_site.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from src.common import (
    clean_name,
    make_downloader,
)
from src.common import (
    client as _shared_client,
)
from term import cinfo, cwarning


class SiteDriver:
    """Base class for site drivers. Subclasses fill in the blanks."""

    # ---- subclass identity (override) -------------------------------------
    key: str = ""
    name: str = ""
    domains: tuple[str, ...] = ()
    referer: str = ""  # default Referer header for this site's requests
    # Content-sniffed resolution: when no driver matches a URL by domain the
    # page is fetched once and every driver's sniff() is asked, highest
    # priority first. Specific drivers (a theme with recognizable markup)
    # sit above 0, the catch-all generic driver far below it.
    priority: int = 0

    # ---- optional tuning (override) ---------------------------------------
    extra_headers: dict[str, str] | None = None

    # -----------------------------------------------------------------------
    # URL handling (override anything that differs)
    # -----------------------------------------------------------------------

    def matches(self, url: str) -> bool:
        """True if this driver handles the URL's domain."""
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def sniff(self, url: str, html: str) -> bool:
        """True if this driver recognizes the fetched page of a URL no driver matched by domain."""
        return False

    def classify(self, url: str) -> str:
        """Return 'list', 'chapter', or 'unknown' for a URL on this site."""
        raise NotImplementedError

    def series_slug(self, url: str) -> str:
        """Extract the series slug from a list or chapter URL."""
        raise NotImplementedError

    def series_folder(self, url: str) -> str:
        """Folder name for a series under downloads/<key>/.

        Routed through `safe()` (clean_name) unconditionally: series_slug()
        is driver-supplied and, for some drivers, built straight from a URL
        query parameter that gets percent-decoded before it ever reaches
        here. Without this, a crafted link could embed a `../` sequence and
        write chapter files outside the downloads tree. This is the single
        chokepoint for every driver, present and future.
        """
        try:
            return self.safe(self.series_slug(url))
        except NotImplementedError:
            return "series"

    def single_chapter_folder(self) -> str:
        return "single_chapters"

    # -----------------------------------------------------------------------
    # Site specifics — the four methods a driver MUST provide
    # -----------------------------------------------------------------------

    def client(self, **kwargs) -> httpx.AsyncClient:
        """AsyncClient preconfigured with this site's headers.

        `referer=` may be passed to override the driver's fixed referer for
        one client -- the facade does that with referer_for(url) so a driver
        without a fixed host can still send the right one.
        """
        referer = kwargs.pop("referer", self.referer or None)
        return _shared_client(
            self.base_url(),
            referer=referer,
            extra_headers=self.extra_headers,
            **kwargs,
        )

    def base_url(self) -> str:
        """Default base url used for the client + referer ('' for domainless drivers)."""
        return f"https://{self.domains[0]}" if self.domains else ""

    def referer_for(self, url: str) -> str | None:
        """Referer for requests made on behalf of `url` (default: the driver's fixed one)."""
        return self.referer or None

    def list_chapters(self, client: httpx.AsyncClient, url: str) -> Awaitable[list[tuple[str, float]]]:
        """All chapters of a series: [(chapter_url, num), ...] oldest first."""
        raise NotImplementedError

    def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> Awaitable[list[str]]:
        """Ordered image urls for one chapter page."""
        raise NotImplementedError

    def folder_name(self, chapter_url: str) -> str:
        """Output folder name for one chapter."""
        raise NotImplementedError

    # -----------------------------------------------------------------------
    # Engine wiring — inherited, drivers normally never touch this
    # -----------------------------------------------------------------------

    @property
    def _download_engine(self) -> Callable[..., Awaitable[dict[str, Any]]]:
        return make_downloader(
            site_label=self.key,
            fetch_image_urls=self.image_urls,
            chapter_folder_name=self.folder_name,
        )

    def select_chapters(self, chapters: list[tuple[str, float]], wanted: list[int] | None) -> list[str]:
        """Filter (url, num) pairs by integer chapter numbers."""
        if wanted is None:
            return [url for url, _ in chapters]
        wanted_set = set(wanted)
        return [url for url, num in chapters if int(num) in wanted_set]

    async def download_series_url(
        self,
        url: str,
        out_dir: Path,
        chapters: list[int] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Facade used by main.py: fetch chapters, filter, download."""
        engine = self._download_engine
        if self.classify(url) == "chapter":
            async with self.client(referer=self.referer_for(url)) as c:
                return await engine(c, [url], out_dir, dry_run=dry_run)

        async with self.client(referer=self.referer_for(url)) as c:
            links = await self.list_chapters(c, url)
            if not links:
                raise RuntimeError("No chapters found on the series page.")
            cinfo(f"Found {len(links)} chapter(s)")
            selected = self.select_chapters(links, chapters)
            if not selected:
                raise RuntimeError("No chapters matched the requested range.")
            return await engine(c, selected, out_dir, dry_run=dry_run)

    async def count_chapters(self, url: str) -> list[tuple[str, float]]:
        """Chapter listing for main.py's pre-download count/range prompt."""
        async with self.client(referer=self.referer_for(url)) as c:
            return await self.list_chapters(c, url)

    # -----------------------------------------------------------------------
    # Shared small helpers drivers can reuse
    # -----------------------------------------------------------------------

    @staticmethod
    def safe(text: str | None) -> str:
        return clean_name(text)

    @staticmethod
    def chapter_label(num: float) -> str:
        """'12' for 12.0, '12.5' for 12.5: a chapter number without a spurious decimal tail."""
        if num.is_integer():
            return str(int(num))
        return f"{num:.3f}".rstrip("0").rstrip(".")

    def chapter_folder(self, num: float | None) -> str:
        """The standard chapter folder name, `num<N>_Chapter <N>` (`num0_Chapter unknown` without a number).

        This is the layout OmniScan's importer reads (see AGENTS.md), so a
        driver that knows nothing but the chapter number should use it.
        """
        if num is None:
            return f"num0_{self.safe('Chapter unknown')}"
        return f"num{num:g}_{self.safe(f'Chapter {self.chapter_label(num)}')}"

    @staticmethod
    async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
        """GET a page and return its body, raising on an HTTP error status."""
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def warn(message: str) -> None:
        cwarning(message)

    @staticmethod
    def info(message: str) -> None:
        cinfo(message)
