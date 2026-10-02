"""MangaDex site driver (official API, no scraping).

mangadex.org is a JavaScript app, so the HTML carries nothing useful; the
public API at api.mangadex.org is the supported way in and documents its
own terms: a descriptive User-Agent, and 429 responses that must be honored
(retry_delay reads their Retry-After). Chapter listing is the manga feed,
paged by its `total`, restricted to the languages in MANGADEX_LANGS
(comma-separated, default `en`) and opened to every content rating so a
series isn't silently truncated. Images come from the MangaDex@Home network:
one call per chapter returns the node to use plus the file names.

Chapter URLs only carry a UUID, but the engine asks for a chapter's folder
name before it fetches that chapter's images -- so the number learned from
the feed (or, for a bare chapter URL, from one extra /chapter/<id> call made
up front) is remembered on the driver and looked up by id.
"""

from __future__ import annotations

import asyncio
import os
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx

from src.base import SiteDriver
from src.common import retry_delay

SITE = "https://mangadex.org"
API = "https://api.mangadex.org"
FEED_PAGE_SIZE = 500
CONTENT_RATINGS = ("safe", "suggestive", "erotica", "pornographic")
API_ATTEMPTS = 6

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TITLE_RE = re.compile(rf"^/title/({_UUID})(?:/[^/]*)?/?$", re.I)
_CHAPTER_RE = re.compile(rf"^/chapter/({_UUID})(?:/\d+)?/?$", re.I)


def user_agent() -> str:
    """Descriptive User-Agent, as MangaDex's API terms ask for."""
    try:
        release = version("manhwa-manga-downloader")
    except PackageNotFoundError:
        release = "dev"
    return f"manhwa-manga-downloader/{release} (+https://github.com/Nawid3333/manhwa-manga-downloader)"


def mangadex_langs() -> list[str]:
    """Translated languages to list, from MANGADEX_LANGS (default: en)."""
    raw = os.environ.get("MANGADEX_LANGS", "")
    langs = [part.strip() for part in raw.split(",") if part.strip()]
    return langs or ["en"]


class MangaDexDriver(SiteDriver):
    key = "mangadex"
    name = "MangaDex (mangadex.org)"
    domains = ("mangadex.org",)
    referer = SITE + "/"
    extra_headers = {"User-Agent": user_agent()}

    def __init__(self) -> None:
        self._numbers: dict[str, float] = {}

    def base_url(self) -> str:
        return SITE

    # ---- URL handling ------------------------------------------------------

    @staticmethod
    def manga_id(url: str) -> str | None:
        m = _TITLE_RE.match(httpx.URL(url).path)
        return m.group(1).lower() if m else None

    @staticmethod
    def chapter_id(url: str) -> str | None:
        m = _CHAPTER_RE.match(httpx.URL(url).path)
        return m.group(1).lower() if m else None

    def classify(self, url: str) -> str:
        if self.chapter_id(url):
            return "chapter"
        if self.manga_id(url):
            return "list"
        return "unknown"

    def series_slug(self, url: str) -> str:
        manga_id = self.manga_id(url)
        if manga_id is None:
            raise ValueError(f"Could not extract series id from {url}")
        return manga_id

    # ---- site specifics ----------------------------------------------------

    def folder_name(self, chapter_url: str) -> str:
        chapter_id = self.chapter_id(chapter_url) or ""
        num = self._numbers.get(chapter_id)
        if num is None:
            label = chapter_id[:8] or "unknown"
            return f"num0_{self.safe(f'Chapter {label}')}"
        return self.chapter_folder(num)

    async def api_get(self, client: httpx.AsyncClient, path: str, params: Any = None) -> dict[str, Any]:
        """GET an API path as JSON, backing off on 429/503 the way the API asks."""
        for attempt in range(API_ATTEMPTS):
            resp = await client.get(API + path, params=params, headers={"Accept": "application/json"})
            if resp.status_code in (429, 503) and attempt < API_ATTEMPTS - 1:
                await asyncio.sleep(retry_delay(resp, attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # pragma: no cover - loop always returns or raises

    @staticmethod
    def chapter_number(record: dict[str, Any]) -> float | None:
        """Numeric chapter from a feed/chapter record; None for oneshots or odd labels."""
        raw = (record.get("attributes") or {}).get("chapter")
        if raw is None:
            return None
        try:
            return float(str(raw))
        except ValueError:
            return None

    @staticmethod
    def is_hosted(record: dict[str, Any]) -> bool:
        """Whether MangaDex itself serves the chapter's pages.

        Officially licensed chapters appear in the feed as links out to the
        publisher (MANGA Plus, ...): `externalUrl` is set and `pages` is 0, and
        /at-home/server has nothing to give for them. Picking such a record
        for a chapter number would leave that chapter empty even when a
        scanlation of the same number is downloadable.
        """
        attributes = record.get("attributes") or {}
        return not attributes.get("externalUrl") and attributes.get("pages") != 0

    async def fetch_feed(self, client: httpx.AsyncClient, manga_id: str) -> list[dict[str, Any]]:
        """Every chapter record of the manga feed, paging by the API's `total`."""
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: list[tuple[str, Any]] = [
                ("limit", FEED_PAGE_SIZE),
                ("offset", offset),
                ("order[chapter]", "asc"),
                *(("translatedLanguage[]", lang) for lang in mangadex_langs()),
                *(("contentRating[]", rating) for rating in CONTENT_RATINGS),
            ]
            data = await self.api_get(client, f"/manga/{manga_id}/feed", params)
            batch = data.get("data") or []
            records.extend(batch)
            offset += len(batch)
            total = int(data.get("total") or 0)
            if not batch or offset >= total:
                return records

    async def list_chapters(self, client: httpx.AsyncClient, list_url: str) -> list[tuple[str, float]]:
        """Return [(chapter_url, num), ...] oldest first; one hosted upload per chapter number."""
        manga_id = self.series_slug(list_url)
        deduped: dict[float, str] = {}
        for rec in await self.fetch_feed(client, manga_id):
            chapter_id = str(rec.get("id") or "").lower()
            num = self.chapter_number(rec)
            if not chapter_id or num is None or num in deduped or not self.is_hosted(rec):
                continue
            deduped[num] = f"{SITE}/chapter/{chapter_id}"
            self._numbers[chapter_id] = num
        return [(url, num) for num, url in sorted(deduped.items())]

    async def remember_chapter(self, client: httpx.AsyncClient, chapter_id: str) -> None:
        """Learn a chapter's number from /chapter/<id> (for a bare chapter URL)."""
        data = await self.api_get(client, f"/chapter/{chapter_id}")
        num = self.chapter_number(data.get("data") or {})
        if num is not None:
            self._numbers[chapter_id] = num

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        """Full-quality page URLs from the MangaDex@Home node assigned to this chapter."""
        chapter_id = self.chapter_id(chapter_url)
        if chapter_id is None:
            return []
        data = await self.api_get(client, f"/at-home/server/{chapter_id}")
        base = str(data.get("baseUrl") or "").rstrip("/")
        chapter = data.get("chapter") or {}
        chapter_hash = chapter.get("hash")
        files = chapter.get("data") or []
        if not base or not chapter_hash:
            return []
        return [f"{base}/data/{chapter_hash}/{name}" for name in files]

    async def download_series_url(
        self,
        url: str,
        out_dir: Path,
        chapters: list[int] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        chapter_id = self.chapter_id(url)
        if chapter_id is not None and chapter_id not in self._numbers:
            async with self.client() as c:
                try:
                    await self.remember_chapter(c, chapter_id)
                except httpx.HTTPError as exc:
                    self.warn(f"Could not read chapter metadata ({exc}); folder will be unnumbered.")
        return await super().download_series_url(url, out_dir, chapters=chapters, dry_run=dry_run)


driver = MangaDexDriver()
