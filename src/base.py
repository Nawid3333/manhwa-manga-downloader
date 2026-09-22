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
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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

    # ---- optional tuning (override) ---------------------------------------
    extra_headers: dict[str, str] | None = None

    # -----------------------------------------------------------------------
    # URL handling (override anything that differs)
    # -----------------------------------------------------------------------

    def matches(self, url: str) -> bool:
        """True if this driver handles the URL's domain."""
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in self.domains)

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
        """AsyncClient preconfigured with this site's headers."""
        return _shared_client(
            self.base_url(),
            referer=self.referer or None,
            extra_headers=self.extra_headers,
            **kwargs,
        )

    def base_url(self) -> str:
        """Default base url used for the client + referer."""
        return f"https://{self.domains[0]}"

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
        out_dir,
        chapters: list[int] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Facade used by main.py: fetch chapters, filter, download."""
        engine = self._download_engine
        if self.classify(url) == "chapter":
            async with self.client() as c:
                return await engine(c, [url], out_dir, dry_run=dry_run)

        async with self.client() as c:
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
        async with self.client() as c:
            return await self.list_chapters(c, url)

    # -----------------------------------------------------------------------
    # Shared small helpers drivers can reuse
    # -----------------------------------------------------------------------

    @staticmethod
    def safe(text: str | None) -> str:
        return clean_name(text)

    @staticmethod
    def warn(message: str) -> None:
        cwarning(message)

    @staticmethod
    def info(message: str) -> None:
        cinfo(message)
