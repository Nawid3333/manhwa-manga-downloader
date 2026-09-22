"""nelomanga.net site driver.

The HTML pages sit behind a Cloudflare JS challenge, but the JSON chapter API
and the image CDN are not. So this driver:

1. lists chapters via /api/manga/{slug}/chapters?limit=50&offset=N
2. builds CDN image URLs from the predictable pattern
     {host}/{series_slug}/{num}/{index}.webp   (0-based)
   where fractional chapters use dotted folders (chapter-194-1 -> 194.1)
   and trailing zeros are kept (chapter-179-0 -> 179.0, distinct from 179).
   Different chapters are served from different CDN hosts, so each chapter is
   probed against a host pool; the page count is found with a HEAD-request
   binary search (the reader HTML itself cannot be fetched without a browser).
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx

from src.base import SiteDriver
from src.common import retry_delay
from term import cwarning

BASE = "https://www.nelomanga.net"
API_CHAPTERS = BASE + "/api/manga/{slug}/chapters"
PAGE_SIZE = 50

# CDN hosts serving chapter images. Chapters live on different hosts, so we
# probe the pool per chapter and use whichever host has the chapter.
CDN_HOSTS = (
    "https://img-r1.2xstorage.com",
    "https://imgs-2.2xstorage.com",
)
IMG_REFERER = BASE + "/"

_SLUG_RE = re.compile(r"^/manga/([^/]+)/?$")
_CHAPTER_RE = re.compile(r"^/manga/[^/]+/chapter-([0-9]+(?:-[0-9]+)*)$")


class NelomangaDriver(SiteDriver):
    key = "nelomanga"
    name = "nelomanga.net (MangaNelo)"
    domains = ("nelomanga.net",)
    referer = IMG_REFERER

    def base_url(self) -> str:
        return BASE

    # ---- URL handling ------------------------------------------------------

    def is_chapter_url(self, url: str) -> bool:
        return _CHAPTER_RE.match(urlparse(url).path) is not None

    def is_list_url(self, url: str) -> bool:
        path = urlparse(url).path
        return _SLUG_RE.match(path) is not None and not self.is_chapter_url(url)

    def classify(self, url: str) -> str:
        if self.is_chapter_url(url):
            return "chapter"
        if self.is_list_url(url):
            return "list"
        return "unknown"

    def series_slug(self, url: str) -> str:
        """Extract the series slug from either a list or a chapter URL."""
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "manga":
            return parts[1]
        raise ValueError(f"Could not extract series slug from {url}")

    # ---- site specifics ----------------------------------------------------

    @staticmethod
    def chapter_label(url: str) -> str | None:
        """Raw CDN label from the URL slug: chapter-194-1 -> '194.1'."""
        m = _CHAPTER_RE.match(urlparse(url).path)
        if not m:
            return None
        return m.group(1).replace("-", ".")

    @classmethod
    def chapter_num(cls, url: str) -> float | None:
        label = cls.chapter_label(url)
        if label is None:
            return None
        try:
            return float(label)
        except ValueError:
            return None

    def folder_name(self, chapter_url: str) -> str:
        label = self.chapter_label(chapter_url)
        if label is None:
            tail = urlparse(chapter_url).path.rsplit("/", 1)[-1]
            return f"num0_{self.safe(tail)}"
        title = f"Chapter {label}"
        return f"num{label}_{self.safe(title)}"

    async def fetch_all_chapters(self, client: httpx.AsyncClient, slug: str) -> list[dict]:
        """Fetch every chapter record from the JSON API (newest first)."""
        chapters: list[dict] = []
        offset = 0
        while True:
            resp = await client.get(
                API_CHAPTERS.format(slug=slug),
                params={"limit": PAGE_SIZE, "offset": offset},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            batch = data.get("chapters", [])
            chapters.extend(batch)
            pagination = data.get("pagination") or {}
            if pagination.get("has_more") and batch:
                offset += PAGE_SIZE
            else:
                break
        return chapters

    async def list_chapters(self, client: httpx.AsyncClient, list_url: str) -> list[tuple[str, float]]:
        """Return [(chapter_url, num), ...] sorted oldest -> newest."""
        slug = self.series_slug(list_url)
        records = await self.fetch_all_chapters(client, slug)
        items: list[tuple[str, str]] = []
        for rec in records:
            ch_slug = str(rec.get("chapter_slug") or "").strip()
            if not ch_slug:
                continue
            url = f"{BASE}/manga/{slug}/{ch_slug}"
            items.append((url, ch_slug.removeprefix("chapter-").replace("-", ".")))
        # Dedupe by exact label string so 179 and 179.0 both survive.
        deduped: dict[str, str] = {}
        for url, label in items:
            deduped.setdefault(label, url)

        def _sort_key(pair: tuple[str, str]) -> tuple[float, str]:
            try:
                return (float(pair[0]), pair[0])
            except ValueError:
                return (float("inf"), pair[0])

        return [(url, float(label)) for label, url in sorted(deduped.items(), key=_sort_key)]

    @staticmethod
    async def _head_ok(client: httpx.AsyncClient, url: str) -> bool:
        # A 429/503 means "the CDN is throttling us", not "this page doesn't
        # exist" — treating it as the latter corrupts the binary-search page
        # count and can make an entire chapter look CDN-unreachable under load.
        for attempt in range(6):
            try:
                resp = await client.head(url)
            except httpx.HTTPError:
                return False
            if resp.status_code == 200:
                return True
            if resp.status_code in (429, 503):
                await asyncio.sleep(retry_delay(resp, attempt))
                continue
            return False
        return False

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        """Probe the CDN pattern for one chapter and return existing image urls."""
        label = self.chapter_label(chapter_url)
        slug = self.series_slug(chapter_url)
        if label is None:
            return []

        # Pick the host that actually serves this chapter.
        chosen: str | None = None
        for host in CDN_HOSTS:
            if await self._head_ok(client, f"{host}/{slug}/{label}/0.webp"):
                chosen = host
                break
        if chosen is None:
            cwarning(f"  CDN unreachable for chapter {label}")
            return []

        # Exponential upper bound for the page count (0-based indices).
        lo, hi = 1, 8
        while await self._head_ok(client, f"{chosen}/{slug}/{label}/{hi}.webp"):
            lo = hi + 1
            hi *= 2
            if hi > 512:
                break

        # Binary search for the first missing index in [lo, hi].
        while lo < hi:
            mid = (lo + hi) // 2
            if await self._head_ok(client, f"{chosen}/{slug}/{label}/{mid}.webp"):
                lo = mid + 1
            else:
                hi = mid
        count = lo  # first missing index == total pages

        return [f"{chosen}/{slug}/{label}/{i}.webp" for i in range(count)]


driver = NelomangaDriver()
