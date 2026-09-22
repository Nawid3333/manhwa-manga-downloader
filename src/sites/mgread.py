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
from bs4 import BeautifulSoup

from src.common import clean_name, make_downloader
from src.common import client as _client
from term import cinfo, cwarning

SITE_KEY = "mgread"
BASE = "https://mgread.io"
IMG_HOST = "https://mg.mgread.io"
IMG_REFERER = BASE + "/"

_SLUG_RE = re.compile(r"^/manga/([^/]+)/?$")
_PAGE_RE = re.compile(r"^/manga/[^/]+/chapter/page/(\d+)/?$")
_CHAPTER_RE = re.compile(r"^/manga/[^/]+/chapter-([0-9.]+?)/?$")


def client(**kwargs):
    """AsyncClient preconfigured for mgread (referers set for the image host)."""
    return _client(BASE, referer=IMG_REFERER, **kwargs)


def is_chapter_url(url: str) -> bool:
    return _CHAPTER_RE.match(urlparse(str(url)).path) is not None


def is_list_url(url: str) -> bool:
    return _SLUG_RE.match(urlparse(str(url)).path) is not None


def series_slug(url: str) -> str:
    m = _SLUG_RE.match(urlparse(url).path)
    if not m:
        raise ValueError(f"Could not extract series slug from {url}")
    return m.group(1)


def chapter_num_from_url(url: str) -> float | None:
    m = _CHAPTER_RE.match(urlparse(url).path)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def chapter_label(num: float) -> str:
    if num.is_integer():
        return str(int(num))
    return f"{num:.3f}".rstrip("0").rstrip(".")


def chapter_folder_name(chapter_url: str) -> str:
    num = chapter_num_from_url(chapter_url)
    label = chapter_label(num) if num is not None else "unknown"
    title = f"Chapter {label}"
    return f"num{num:g}_{clean_name(title)}" if num is not None else f"num0_{clean_name(title)}"


async def _page_html(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


def parse_chapter_links(html: str, base_url: str) -> list[tuple[str, float]]:
    """Extract (chapter_url, num) pairs from a series page."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, float]] = []
    seen: set[float] = set()
    for a in soup.select("#chapter-list a[href]"):
        href = str(a.get("href", ""))
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


def parse_pagination_pages(html: str) -> list[int]:
    """Return every page number 1..max seen in the pagination block.

    The widget truncates with an ellipsis (1, 2, 3, ..., 9), so intermediate
    pages must be inferred by filling the gap up to the max.
    """
    soup = BeautifulSoup(html, "lxml")
    pages = {1}
    for a in soup.select("nav[aria-label='Pagination'] a[href]"):
        m = _PAGE_RE.match(urlparse(str(a.get("href", ""))).path)
        if m:
            pages.add(int(m.group(1)))
    return list(range(1, max(pages) + 1))


def parse_manga_id(html: str) -> str | None:
    el = BeautifulSoup(html, "lxml").select_one("[data-manga-id]")
    if el is None:
        return None
    val = el.get("data-manga-id")
    return str(val).strip() if val else None


async def fetch_all_chapter_urls(client: httpx.AsyncClient, list_url: str) -> list[tuple[str, float]]:
    """Return [(chapter_url, num), ...] sorted oldest -> newest."""
    slug = series_slug(list_url)
    first_html = await _page_html(client, list_url)

    pages = parse_pagination_pages(first_html)
    semaphore = asyncio.Semaphore(4)

    async def _fetch_page(p: int) -> list[tuple[str, float]]:
        async with semaphore:
            html = await _page_html(client, f"{BASE}/manga/{slug}/chapter/page/{p}/")
            return parse_chapter_links(html, BASE)

    results = await asyncio.gather(*(_fetch_page(p) for p in pages), return_exceptions=True)
    chapters: list[tuple[str, float]] = parse_chapter_links(first_html, BASE)
    for result in results:
        if isinstance(result, BaseException):
            cwarning(f"Failed to fetch a list page: {result}")
            continue
        chapters.extend(result)

    deduped: dict[float, str] = {}
    for url, num in chapters:
        deduped.setdefault(num, url)
    return [(url, num) for num, url in sorted(deduped.items())]


async def fetch_image_urls(client: httpx.AsyncClient, chapter_url: str) -> list[str]:
    """Scrape reader page and return image urls (data-original-src preferred)."""
    if not chapter_url.endswith("/"):
        chapter_url += "/"
    html = await _page_html(client, chapter_url)
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for img in soup.find_all("img"):
        src = str(img.get("data-original-src") or img.get("src") or "")
        if not src:
            continue
        if src.startswith("data:") or "mg.mgread.io" not in src:
            continue
        urls.append(src)
    return urls


download_series = make_downloader(
    site_label="mgread",
    fetch_image_urls=fetch_image_urls,
    chapter_folder_name=chapter_folder_name,
)


def select_chapters(chapters: list[tuple[str, float]], wanted: list[int] | None) -> list[str]:
    if wanted is None:
        return [url for url, _ in chapters]
    wanted_set = set(wanted)
    return [url for url, num in chapters if int(num) in wanted_set]


async def download_series_url(
    url: str,
    out_dir,
    chapters: list[int] | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Facade used by main.py: fetch chapters, filter, and download."""
    if is_chapter_url(url):
        async with client() as c:
            return await download_series(c, [url], out_dir, dry_run=dry_run)
    async with _client(BASE, referer=BASE + "/") as c:
        links = await fetch_all_chapter_urls(c, url)
        if not links:
            raise RuntimeError("No chapters found on the series page.")
        cinfo(f"Found {len(links)} chapter(s)")
        selected = select_chapters(links, chapters)
        if not selected:
            raise RuntimeError("No chapters matched the requested range.")
        return await download_series(c, selected, out_dir, dry_run=dry_run)
