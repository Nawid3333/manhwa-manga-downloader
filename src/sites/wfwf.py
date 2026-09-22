"""wfwf504.com (늑대닷컴) site driver.

Korean site; the list pages are plain HTML paginated at /list?toon=ID&s=o&pg=N
and chapter viewers put images in #vimg-area with data-src preferred.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from src.base import SiteDriver
from src.common import CHAPTER_PAGE_CONCURRENCY
from src.htmlutil import all_of, attr, first, parse_html, spaced_text
from term import cerror

BASE = "https://wfwf504.com"


class WfwfDriver(SiteDriver):
    key = "wfwf504"
    name = "wfwf504.com (늑대닷컴)"
    domains = ("wfwf504.com",)
    referer = f"{BASE}/"

    def base_url(self) -> str:
        return BASE

    # ---- URL handling ------------------------------------------------------

    def is_list_url(self, url: str) -> bool:
        return "/list" in urlparse(url).path

    def is_chapter_url(self, url: str) -> bool:
        return "/view" in urlparse(url).path

    def classify(self, url: str) -> str:
        if self.is_chapter_url(url):
            return "chapter"
        if self.is_list_url(url):
            return "list"
        return "unknown"

    def series_slug(self, url: str) -> str:
        qs = parse_qs(urlparse(url).query)
        toon = qs.get("toon", [""])[0]
        if not toon:
            raise ValueError(f"Could not extract series slug from {url}")
        return toon

    # ---- site specifics ----------------------------------------------------

    def folder_name(self, chapter_url: str) -> str:
        qs = parse_qs(urlparse(chapter_url).query)
        num = qs.get("num", ["unknown"])[0]
        return f"num{num}_chapter"

    async def list_chapters(self, client: httpx.AsyncClient, list_url: str) -> list[tuple[str, float]]:
        """All chapters across pagination pages, sorted oldest -> newest."""
        slug = self.series_slug(list_url)
        qs = parse_qs(urlparse(list_url).query)
        sort = qs.get("s", ["o"])[0] or "o"

        first_html = await self._fetch_text(client, self._list_url(slug, sort, 1))
        pages = self._parse_pagination_pages(first_html)

        semaphore = asyncio.Semaphore(CHAPTER_PAGE_CONCURRENCY)

        async def _page(p: int) -> list[tuple[str, str]]:
            async with semaphore:
                html = await self._fetch_text(client, self._list_url(slug, sort, p))
                return self._parse_chapter_links(html)

        results = await asyncio.gather(*(_page(p) for p in pages), return_exceptions=True)
        all_links: list[tuple[str, str]] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, BaseException):
                cerror(f"Failed to fetch a list page: {result}")
                continue
            for href, title in result:
                if href in seen:
                    continue
                seen.add(href)
                all_links.append((href, title))
        return [(self._normalize(href), self._num_from_title(title)) for href, title in all_links]

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        html = await self._fetch_text(client, chapter_url)
        return self._parse_image_urls(html)

    # ---- internals ---------------------------------------------------------

    async def _fetch_text(self, client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _list_url(slug: str, sort: str = "o", page: int = 1) -> str:
        params = {"toon": slug, "s": sort, "pg": str(page)}
        return f"{BASE}/list?{urlencode(params)}"

    @staticmethod
    def _normalize(href: str) -> str:
        if href.startswith("http"):
            return href
        return f"{BASE}{href if href.startswith('/') else '/' + href}"

    @staticmethod
    def _num_from_title(title: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)", title or "")
        if m:
            return float(m.group(1))
        return 0.0

    @staticmethod
    def _parse_chapter_links(html: str) -> list[tuple[str, str]]:
        doc = parse_html(html)
        links: list[tuple[str, str]] = []
        for a in all_of(
            doc,
            "//*[contains(@class,'list-sec')]//a[@href]",
        ):
            classes = (attr(a, "class") or "").split()
            if "ep-item" not in classes:
                continue
            href = attr(a, "href")
            if href and href.strip():
                title = spaced_text(a)
                links.append((href.strip(), title))
        return links

    @staticmethod
    def _parse_pagination_pages(html: str) -> list[int]:
        doc = parse_html(html)
        pages: set[int] = {1}
        for a in all_of(doc, "//*[contains(@class,'pagi-wrap')]//a[@href]"):
            classes = (attr(a, "class") or "").split()
            if "pg-btn" not in classes:
                continue
            href = attr(a, "href") or ""
            qs = parse_qs(urlparse(href).query)
            pg = qs.get("pg") or qs.get("page")
            if pg:
                try:
                    pages.add(int(pg[0]))
                except ValueError:
                    continue
        return sorted(pages)

    @staticmethod
    def _parse_image_urls(html: str) -> list[str]:
        doc = parse_html(html)
        area = first(doc, "//*[@id='vimg-area']")
        if area is None:
            return []
        images: list[str] = []
        for img in all_of(area, ".//img"):
            for name in ("data-src", "data-original", "src"):
                src = attr(img, name)
                if src and src.strip():
                    images.append(src.strip())
                    break
        return images


driver = WfwfDriver()
