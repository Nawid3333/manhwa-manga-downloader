"""Shared helpers for site drivers.

Contains the pieces every site driver needs: the async HTTP client factory,
concurrency limits, filename sanitizing, and a reusable concurrent chapter
download engine driven by site-specific "get image urls" callables.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from term import cerror, cinfo, cwarning

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 60.0
IMAGE_CONCURRENCY = 24
CHAPTER_PAGE_CONCURRENCY = 6
CHAPTER_DOWNLOAD_CONCURRENCY = 5

ILLEGAL_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_name(text: str | None) -> str:
    """Sanitize a string for use in folder/file names on Windows."""
    if not text:
        return "untitled"
    cleaned = ILLEGAL_NAME_RE.sub("", str(text)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(". ")
    return cleaned[:180] or "untitled"


def limits() -> httpx.Limits:
    return httpx.Limits(max_keepalive_connections=20, max_connections=100)


def client(
    base_site: str,
    *,
    referer: str | None = None,
    accept: str | None = None,
    extra_headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Create a shared AsyncClient with browser-like defaults."""
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    if accept:
        headers["Accept"] = accept
    headers["Accept-Language"] = "en-US,en;q=0.9"
    if extra_headers:
        headers.update(extra_headers)
    merged: dict[str, Any] = {
        "headers": headers,
        "timeout": HTTP_TIMEOUT,
        "follow_redirects": True,
        "http2": True,
        "limits": limits(),
    }
    merged.update(kwargs)
    return httpx.AsyncClient(**merged)


def make_downloader(
    *,
    site_label: str,
    fetch_image_urls: Callable[[httpx.AsyncClient, str], Awaitable[list[str]]],
    chapter_folder_name: Callable[[str], str],
) -> Callable[..., Awaitable[dict[str, int]]]:
    """Build a download_series function from site-specific pieces.

    fetch_image_urls(client, chapter_url) -> ordered image urls for one chapter
    chapter_folder_name(chapter_url) -> folder name for that chapter
    """

    async def download_image(client: httpx.AsyncClient, url: str, dest: Path, image_sem: asyncio.Semaphore) -> bool:
        # CDNs occasionally drop HTTP/2 streams under load; retry transport
        # failures a few times and write via a .part file so partial downloads
        # are never mistaken for complete ones.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with image_sem:
                    if dest.exists():
                        return True
                    async with client.stream("GET", url, timeout=120) as resp:
                        resp.raise_for_status()
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        tmp = dest.with_suffix(dest.suffix + ".part")
                        with tmp.open("wb") as out:
                            async for chunk in resp.aiter_bytes(64 * 1024):
                                out.write(chunk)
                        tmp.replace(dest)
                return True
            except (httpx.HTTPError, OSError) as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        cwarning(f"      image failed {url}: {last_exc}")
        return False

    async def download_chapter(
        client: httpx.AsyncClient,
        chapter_url: str,
        out_dir: Path,
        chapter_sem: asyncio.Semaphore,
        image_sem: asyncio.Semaphore,
        dry_run: bool = False,
    ) -> int:
        async with chapter_sem:
            folder_name = chapter_folder_name(chapter_url)
            try:
                image_urls = await fetch_image_urls(client, chapter_url)
            except httpx.HTTPError as exc:
                cwarning(f"  failed to read {folder_name}: {exc}")
                return 0
            if not image_urls:
                cwarning(f"  no images in {folder_name} ({chapter_url})")
                return 0

            if dry_run:
                cinfo(f"  [dry-run] {folder_name}: {len(image_urls)} images")
                return len(image_urls)

            cinfo(f"  downloading {folder_name}: {len(image_urls)} images")
            folder = out_dir / folder_name
            folder.mkdir(parents=True, exist_ok=True)

            async def _one(idx_url: tuple[int, str]) -> bool:
                idx, url = idx_url
                ext = Path(urlparse(url).path).suffix.lower() or ".jpg"
                dest = folder / f"{idx:04d}{ext}"
                return await download_image(client, url, dest, image_sem)

            results = await asyncio.gather(
                *(_one(pair) for pair in enumerate(image_urls, 1)),
                return_exceptions=True,
            )
            ok = sum(1 for r in results if r is True)
            failed = len(results) - ok
            if failed:
                cwarning(f"    {failed}/{len(image_urls)} images failed in {folder_name}")
            return ok

    async def download_series(
        client: httpx.AsyncClient,
        chapter_urls: list[str],
        out_dir: Path,
        *,
        dry_run: bool = False,
    ) -> dict[str, int]:
        image_sem = asyncio.Semaphore(IMAGE_CONCURRENCY)
        chapter_sem = asyncio.Semaphore(CHAPTER_DOWNLOAD_CONCURRENCY)
        out_dir.mkdir(parents=True, exist_ok=True)

        results = await asyncio.gather(
            *(download_chapter(client, url, out_dir, chapter_sem, image_sem, dry_run) for url in chapter_urls),
            return_exceptions=True,
        )
        total_ok = 0
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                cerror(f"Chapter failed: {result}")
                failures += 1
                continue
            total_ok += result
        return {
            "chapters": len(chapter_urls) - failures,
            "images": total_ok,
            "failed_chapters": failures,
        }

    return download_series
