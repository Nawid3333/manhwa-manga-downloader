"""wfwf504.com (늑대닷컴) site driver."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from src.htmlutil import all_of, has_class, parse_html, spaced_text
from term import cerror, cinfo, cwarning

SITE_KEY = "wfwf504"
BASE_DOMAIN = "wfwf504.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 60.0
IMAGE_CONCURRENCY = 32
CHAPTER_PAGE_CONCURRENCY = 8
CHAPTER_DOWNLOAD_CONCURRENCY = 5

ILLEGAL_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean(text: str | None) -> str:
    if not text:
        return "untitled"
    cleaned = ILLEGAL_NAME_RE.sub("", str(text)).strip()
    # Windows drops trailing dots/spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip(". ")
    return cleaned[:180] or "untitled"


def _chapter_folder(num: int, title: str) -> str:
    return f"num{num}_{_clean(title)}"


def limits() -> httpx.Limits:
    return httpx.Limits(max_keepalive_connections=20, max_connections=100)


def client(**kwargs: Any) -> httpx.AsyncClient:
    merged = {
        "headers": {"User-Agent": USER_AGENT, "Referer": f"https://{BASE_DOMAIN}/"},
        "timeout": HTTP_TIMEOUT,
        "follow_redirects": True,
        "http2": True,
        "limits": limits(),
    }
    merged.update(kwargs)
    return httpx.AsyncClient(**merged)


def is_list_url(url: str) -> bool:
    return "/list" in url


def is_chapter_url(url: str) -> bool:
    return "/view" in url


def series_slug(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    toon = qs.get("toon", [""])[0]
    if not toon:
        raise ValueError(f"Could not extract series slug from {url}")
    return toon


def list_url_for_slug(slug: str, *, sort: str = "o", page: int = 1) -> str:
    params = {"toon": slug, "s": sort, "pg": str(page)}
    return f"https://{BASE_DOMAIN}/list?{urlencode(params)}"


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


# ── Selectors, as XPath ─────────────────────────────────────────────────────
# Literal translations of the CSS selectors the BeautifulSoup version used.
# `#vimg-area` is an id lookup; `.list-sec a.ep-item` and `.pagi-wrap a.pg-btn`
# go through has_class so a class token like `ep-item2` cannot false-match.

_XP_VIEWER_IMAGES = "//*[@id='vimg-area']//img"
_XP_CHAPTER_LINKS = f".//div[{has_class('list-sec')}]//a[{has_class('ep-item')}][@href]"
_XP_PAGINATION = f".//div[{has_class('pagi-wrap')}]//a[{has_class('pg-btn')}][@href]"


def parse_image_urls(html: str) -> list[str]:
    doc = parse_html(html)
    images: list[str] = []
    for img in all_of(doc, _XP_VIEWER_IMAGES):
        for attr in ("data-src", "data-original", "src"):
            src = img.get(attr)
            if isinstance(src, str) and src.strip():
                images.append(src.strip())
                break
    return images


def parse_chapter_links(html: str) -> list[tuple[str, str]]:
    """Return list of (href, title_text) for chapter links on a list page."""
    links: list[tuple[str, str]] = []
    for a in all_of(parse_html(html), _XP_CHAPTER_LINKS):
        href = a.get("href")
        if isinstance(href, str) and href.strip():
            links.append((href.strip(), spaced_text(a)))
    return links


def parse_pagination_pages(html: str, slug: str, sort: str = "o") -> list[int]:
    """Return every page number >= 1 seen on the pagination block."""
    pages: set[int] = {1}
    for a in all_of(parse_html(html), _XP_PAGINATION):
        href = a.get("href", "")
        if not isinstance(href, str):
            continue
        qs = parse_qs(urlparse(href).query)
        pg = qs.get("pg") or qs.get("page")
        if pg:
            try:
                pages.add(int(pg[0]))
            except ValueError:
                continue
    return sorted(pages)


async def fetch_list_page(client: httpx.AsyncClient, slug: str, page: int, sort: str = "o") -> list[tuple[str, str]]:
    url = list_url_for_slug(slug, sort=sort, page=page)
    cinfo(f"  fetching list page {page}: {url}")
    html = await fetch_text(client, url)
    return parse_chapter_links(html)


async def fetch_all_chapter_links(client: httpx.AsyncClient, list_url: str) -> list[tuple[str, str]]:
    slug = series_slug(list_url)
    parsed = urlparse(list_url)
    qs = parse_qs(parsed.query)
    sort = qs.get("s", ["o"])[0] or "o"

    first_html = await fetch_text(client, list_url_for_slug(slug, sort=sort, page=1))
    pages = parse_pagination_pages(first_html, slug, sort)

    semaphore = asyncio.Semaphore(CHAPTER_PAGE_CONCURRENCY)

    async def _page(p: int) -> list[tuple[str, str]]:
        async with semaphore:
            return await fetch_list_page(client, slug, p, sort)

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
    return all_links


def chapter_number_from_title(title: str) -> int:
    """Try to infer a chapter number from the title text."""
    m = re.search(r"(\d+(?:\.\d+)?)", title)
    if m:
        num = float(m.group(1))
        return int(num) if num.is_integer() else int(num * 100)
    return 0


def normalize_chapter_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return f"https://{BASE_DOMAIN}{href if href.startswith('/') else '/' + href}"


async def download_image(client: httpx.AsyncClient, url: str, dest: Path) -> bool:
    try:
        async with client.stream("GET", url, timeout=120) as resp:
            resp.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    out.write(chunk)
        return True
    except (httpx.HTTPError, OSError) as exc:
        cwarning(f"      image failed {url}: {exc}")
        return False


async def download_chapter(
    client: httpx.AsyncClient,
    chapter_url: str,
    title: str,
    out_dir: Path,
    chapter_semaphore: asyncio.Semaphore,
    image_semaphore: asyncio.Semaphore,
    dry_run: bool = False,
) -> int:
    async with chapter_semaphore:
        chapter_num = chapter_number_from_title(title)
        folder = out_dir / _chapter_folder(chapter_num, title)

        html = await fetch_text(client, chapter_url)
        image_urls = parse_image_urls(html)
        if not image_urls:
            cwarning(f"  no images in {title} ({chapter_url})")
            return 0

        if dry_run:
            cinfo(f"  [dry-run] {title}: {len(image_urls)} images")
            return len(image_urls)

        cinfo(f"  downloading {title}: {len(image_urls)} images → {folder}")
        folder.mkdir(parents=True, exist_ok=True)

        async def _image(i_url: str, idx: int) -> bool:
            ext = Path(urlparse(i_url).path).suffix.lower() or ".jpg"
            dest = folder / f"{idx:04d}{ext}"
            if dest.exists():
                return True
            async with image_semaphore:
                return await download_image(client, i_url, dest)

        results = await asyncio.gather(
            *(_image(url, idx) for idx, url in enumerate(image_urls, 1)),
            return_exceptions=True,
        )
        ok = sum(1 for r in results if r is True)
        failed = len(results) - ok
        if failed:
            cwarning(f"    {failed}/{len(image_urls)} images failed")
        return ok


async def download_series(
    list_url: str,
    out_dir: Path,
    chapters: list[int] | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    async with client() as c:
        links = await fetch_all_chapter_links(c, list_url)
        if not links:
            raise RuntimeError("No chapters found on the list page.")

        cinfo(f"Found {len(links)} chapter(s)")
        if chapters is not None:
            selected = [(href, title) for href, title in links if chapter_number_from_title(title) in chapters]
        else:
            selected = links

        if not selected:
            raise RuntimeError("No chapters matched the requested range.")

        image_sem = asyncio.Semaphore(IMAGE_CONCURRENCY)
        chapter_sem = asyncio.Semaphore(CHAPTER_DOWNLOAD_CONCURRENCY)
        out_dir.mkdir(parents=True, exist_ok=True)

        results = await asyncio.gather(
            *(
                download_chapter(
                    c,
                    normalize_chapter_url(href),
                    title,
                    out_dir,
                    chapter_sem,
                    image_sem,
                    dry_run=dry_run,
                )
                for href, title in selected
            ),
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
            "chapters": len(selected) - failures,
            "images": total_ok,
            "failed_chapters": failures,
        }
