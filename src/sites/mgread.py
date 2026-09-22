"""mgread.io site driver.

Plain WordPress-style site reachable with httpx. Series pages list chapters
paginated at /manga/{slug}/chapter/page/N/ and readers embed the images at
mg.mgread.io/{manga_id}/{num}/{index}.jpg (1-based, referer required).
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx

from src.base import SiteDriver
from src.htmlutil import all_of, attr, parse_html
from term import cwarning

BASE = "https://mgread.io"
IMG_HOST = "https://mg.mgread.io"
IMG_REFERER = BASE + "/"

_SLUG_RE = re.compile(r"^/manga/([^/]+)/?$")
_PAGE_RE = re.compile(r"^/manga/[^/]+/chapter/page/(\d+)/?$")
_CHAPTER_RE = re.compile(r"^/manga/[^/]+/chapter-([0-9.]+?)/?$")


class MgreadDriver(SiteDriver):
    key = "mgread"
    name = "mgread.io"
    domains = ("mgread.io",)
    referer = IMG_REFERER

    def base_url(self) -> str:
        return BASE

    # ---- URL handling ------------------------------------------------------

    def is_chapter_url(self, url: str) -> bool:
        return _CHAPTER_RE.match(urlparse(str(url)).path) is not None

    def is_list_url(self, url: str) -> bool:
        return _SLUG_RE.match(urlparse(str(url)).path) is not None

    def classify(self, url: str) -> str:
        if self.is_chapter_url(url):
            return "chapter"
        if self.is_list_url(url):
            return "list"
        return "unknown"

    def series_slug(self, url: str) -> str:
        m = _SLUG_RE.match(urlparse(url).path)
        if not m:
            raise ValueError(f"Could not extract series slug from {url}")
        return m.group(1)

    # ---- site specifics ----------------------------------------------------

    @staticmethod
    def chapter_num_from_url(url: str) -> float | None:
        m = _CHAPTER_RE.match(urlparse(str(url)).path)
        if not m:
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    @staticmethod
    def chapter_label(num: float) -> str:
        if num.is_integer():
            return str(int(num))
        return f"{num:.3f}".rstrip("0").rstrip(".")

    def folder_name(self, chapter_url: str) -> str:
        num = self.chapter_num_from_url(chapter_url)
        label = self.chapter_label(num) if num is not None else "unknown"
        title = f"Chapter {label}"
        return f"num{num:g}_{self.safe(title)}" if num is not None else f"num0_{self.safe(title)}"

    async def list_chapters(self, client: httpx.AsyncClient, list_url: str) -> list[tuple[str, float]]:
        """Return [(chapter_url, num), ...] sorted oldest -> newest."""
        slug = self.series_slug(list_url)
        first_html = await self._page_html(client, list_url)

        pages = self.parse_pagination_pages(first_html)
        semaphore = asyncio.Semaphore(4)

        async def _fetch_page(p: int) -> list[tuple[str, float]]:
            async with semaphore:
                html = await self._page_html(client, f"{BASE}/manga/{slug}/chapter/page/{p}/")
                return self.parse_chapter_links(html)

        results = await asyncio.gather(*(_fetch_page(p) for p in pages), return_exceptions=True)
        chapters: list[tuple[str, float]] = self.parse_chapter_links(first_html)
        for result in results:
            if isinstance(result, BaseException):
                cwarning(f"Failed to fetch a list page: {result}")
                continue
            chapters.extend(result)

        deduped: dict[float, str] = {}
        for url, num in chapters:
            deduped.setdefault(num, url)
        return [(url, num) for num, url in sorted(deduped.items())]

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        """Scrape reader page and return image urls (data-original-src preferred)."""
        if not chapter_url.endswith("/"):
            chapter_url += "/"
        html = await self._page_html(client, chapter_url)
        doc = parse_html(html)
        urls: list[str] = []
        for img in all_of(doc, "//img"):
            src = attr(img, "data-original-src") or attr(img, "src") or ""
            if not src or src.startswith("data:") or IMG_HOST not in src:
                continue
            urls.append(src)
        return urls

    # ---- page parsing helpers ----------------------------------------------

    @staticmethod
    async def _page_html(client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def parse_chapter_links(html: str) -> list[tuple[str, float]]:
        """Extract (chapter_url, num) pairs from a series page."""
        doc = parse_html(html)
        out: list[tuple[str, float]] = []
        seen: set[float] = set()
        for a in all_of(doc, "//div[@id='chapter-list']//a[@href]"):
            href = attr(a, "href") or ""
            if not href or "/chapter-" not in href:
                continue
            path = urlparse(href).path
            m = _CHAPTER_RE.match(path)
            if not m:
                continue
            try:
                num = float(m.group(1))
            except ValueError:
                continue
            if num in seen:
                continue
            seen.add(num)
            out.append((f"{BASE}{path}", num))
        return out

    @staticmethod
    def parse_pagination_pages(html: str) -> list[int]:
        """Return every page number 1..max seen in the pagination block.

        The widget truncates with an ellipsis (1, 2, 3, ..., 9), so intermediate
        pages must be inferred by filling the gap up to the max.
        """
        doc = parse_html(html)
        pages = {1}
        for a in all_of(doc, "//nav[@aria-label='Pagination']//a[@href]"):
            m = _PAGE_RE.match(urlparse(attr(a, "href") or "").path)
            if m:
                pages.add(int(m.group(1)))
        return list(range(1, max(pages) + 1))


driver = MgreadDriver()
