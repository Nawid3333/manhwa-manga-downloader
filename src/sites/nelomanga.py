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
HEAD_ATTEMPTS = 6

# CDN hosts serving chapter images. Chapters live on different hosts, so we
# probe the pool per chapter and use whichever host has the chapter.
CDN_HOSTS = (
    "https://img-r1.2xstorage.com",
    "https://imgs-2.2xstorage.com",
)
IMG_REFERER = BASE + "/"

_SLUG_RE = re.compile(r"^/manga/([^/]+)/?$")
_CHAPTER_RE = re.compile(r"^/manga/[^/]+/chapter-([0-9]+(?:-[0-9]+)*)/?$")


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
    def cdn_label(url: str) -> str | None:
        """Raw CDN path label from the URL slug: chapter-194-1 -> '194.1' (trailing zeros kept)."""
        m = _CHAPTER_RE.match(urlparse(url).path)
        if not m:
            return None
        return m.group(1).replace("-", ".")

    @classmethod
    def chapter_num(cls, url: str) -> float | None:
        label = cls.cdn_label(url)
        if label is None:
            return None
        try:
            return float(label)
        except ValueError:
            return None

    def folder_name(self, chapter_url: str) -> str:
        label = self.cdn_label(chapter_url)
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
        # Dedupe by exact label string so 179 and 179.0 both survive.
        deduped: dict[str, str] = {}
        for rec in records:
            ch_slug = str(rec.get("chapter_slug") or "").strip()
            url = f"{BASE}/manga/{slug}/{ch_slug}"
            # Only slugs the CDN pattern can serve (chapter-12, chapter-194-1);
            # one like "chapter-extra" has no number to build a URL from, and
            # neither does "chapter-12-1-2" (12.1.2): float() on either would
            # fail the whole listing.
            if ch_slug and self.chapter_num(url) is not None:
                deduped.setdefault(ch_slug.removeprefix("chapter-").replace("-", "."), url)
        return [(url, float(label)) for label, url in sorted(deduped.items(), key=lambda p: (float(p[0]), p[0]))]

    @staticmethod
    async def _head(client: httpx.AsyncClient, url: str, *, retry_transport: bool) -> bool:
        """True for a 200, False for a definite miss (404 and the like).

        A 429/503 means "the CDN is throttling us", not "this page doesn't
        exist" -- treating it as the latter corrupts the binary-search page
        count and can make an entire chapter look CDN-unreachable under load.
        Raises httpx.HTTPError when no definite answer came: a transport
        error (retried first when `retry_transport`), or 429/503 past every
        retry.
        """
        last_exc: httpx.HTTPError | None = None
        for attempt in range(HEAD_ATTEMPTS):
            try:
                resp = await client.head(url)
            except httpx.HTTPError as exc:
                if not retry_transport:
                    raise
                last_exc = exc
                await asyncio.sleep(min(2.0 * (attempt + 1), 15.0))
                continue
            if resp.status_code == 200:
                return True
            if resp.status_code in (429, 503):
                last_exc = httpx.HTTPStatusError(
                    f"HEAD {url} still answered {resp.status_code} after {HEAD_ATTEMPTS} attempts",
                    request=resp.request,
                    response=resp,
                )
                await asyncio.sleep(retry_delay(resp, attempt))
                continue
            return False
        assert last_exc is not None
        raise last_exc

    @classmethod
    async def _head_ok(cls, client: httpx.AsyncClient, url: str) -> bool:
        """Whether a CDN host serves `url`; no definite answer counts as no (it picks a host)."""
        try:
            return await cls._head(client, url, retry_transport=False)
        except httpx.HTTPError:
            return False

    @classmethod
    async def _page_exists(cls, client: httpx.AsyncClient, url: str) -> bool:
        """Whether page `url` exists on the chosen host; raises when the CDN gives no definite answer.

        Counting pages by binary search reads every "no" as "past the last
        page", so a dropped connection or a throttled answer must never count
        as one: it would cut the chapter short, and the engine would then
        record that shorter chapter as complete. Raising instead lets the
        engine retry the whole listing.
        """
        return await cls._head(client, url, retry_transport=True)

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        """Probe the CDN pattern for one chapter and return existing image urls."""
        label = self.cdn_label(chapter_url)
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
        while await self._page_exists(client, f"{chosen}/{slug}/{label}/{hi}.webp"):
            lo = hi + 1
            hi *= 2
            if hi > 512:
                break

        # Binary search for the first missing index in [lo, hi].
        while lo < hi:
            mid = (lo + hi) // 2
            if await self._page_exists(client, f"{chosen}/{slug}/{label}/{mid}.webp"):
                lo = mid + 1
            else:
                hi = mid
        count = lo  # first missing index == total pages

        return [f"{chosen}/{slug}/{label}/{i}.webp" for i in range(count)]


driver = NelomangaDriver()
