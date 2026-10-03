"""늑대닷컴 (wfwf) site driver.

Korean site; the list pages are plain HTML paginated at /list?toon=ID&s=o&pg=N
and chapter viewers put images in #vimg-area with data-src preferred.

The site moves to a new numbered address from time to time (wfwf504.com,
then wfwf510.com); the old one then only serves a notice naming the new
one. Any wfwf<N>.com address is accepted, and every URL is built on the
address the user gave. The key stays "wfwf504", the folder name under
downloads/ and the `site` of the --json result, so earlier downloads resume.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from src.base import SiteDriver
from src.common import CHAPTER_PAGE_CONCURRENCY
from src.htmlutil import all_of, attr, first, parse_html, spaced_text
from term import cerror

BASE = "https://wfwf510.com"  # the current address, as of 2026-10

_HOST_RE = re.compile(r"^(?:www\.)?wfwf\d+\.com$")


def _origin(url: str) -> str:
    """`https://host` of a URL on the site; the current address when it names no wfwf host."""
    parsed = urlparse(url)
    if _HOST_RE.match(parsed.netloc.lower()):
        return f"{parsed.scheme or 'https'}://{parsed.netloc}"
    return BASE


class WfwfDriver(SiteDriver):
    key = "wfwf504"
    name = "늑대닷컴 (wfwf<N>.com)"
    domains = ("wfwf510.com",)
    referer = f"{BASE}/"

    def base_url(self) -> str:
        return BASE

    # ---- URL handling ------------------------------------------------------

    def matches(self, url: str) -> bool:
        return _HOST_RE.match(urlparse(url).netloc.lower()) is not None

    def referer_for(self, url: str) -> str | None:
        return f"{_origin(url)}/"

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
        origin = _origin(list_url)
        qs = parse_qs(urlparse(list_url).query)
        sort = qs.get("s", ["o"])[0] or "o"

        first_html = await self.fetch_text(client, self._list_url(slug, sort, 1, origin))
        self._raise_if_moved(first_html, list_url)
        pages = self._parse_pagination_pages(first_html)

        semaphore = asyncio.Semaphore(CHAPTER_PAGE_CONCURRENCY)

        async def _page(p: int) -> list[tuple[str, str]]:
            async with semaphore:
                html = await self.fetch_text(client, self._list_url(slug, sort, p, origin))
                return self._parse_chapter_links(html)

        # Page 1 is already in hand: only the pages after it are fetched.
        rest = await asyncio.gather(*(_page(p) for p in pages if p != 1), return_exceptions=True)
        results = [self._parse_chapter_links(first_html), *rest]
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
        return [(self._normalize(href, origin), self._num_from_title(title)) for href, title in all_links]

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        html = await self.fetch_text(client, chapter_url)
        self._raise_if_moved(html, chapter_url)
        return self._parse_image_urls(html)

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _raise_if_moved(html: str, url: str) -> None:
        """Stop on the notice an old address serves, naming the new one, instead of finding nothing."""
        doc = parse_html(html)
        if first(doc, "//*[contains(@class,'list-sec') or @id='vimg-area']") is not None:
            return
        for a in all_of(doc, "//a[@href]"):
            target = urlparse(urljoin(url, attr(a, "href") or ""))
            if _HOST_RE.match(target.netloc.lower()) and target.netloc.lower() != urlparse(url).netloc.lower():
                raise RuntimeError(
                    f"{urlparse(url).netloc} has moved to {target.scheme}://{target.netloc}: "
                    "use the same link on that address"
                )

    @staticmethod
    def _list_url(slug: str, sort: str = "o", page: int = 1, origin: str = BASE) -> str:
        params = {"toon": slug, "s": sort, "pg": str(page)}
        return f"{origin}/list?{urlencode(params)}"

    @staticmethod
    def _normalize(href: str, origin: str = BASE) -> str:
        if href.startswith("http"):
            return href
        return f"{origin}{href if href.startswith('/') else '/' + href}"

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
