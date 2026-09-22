"""Async downloader for wfwf504.com manhwa chapters using httpx + asyncio.

Usage:
  python download_wfwf_async.py <chapter_url> [output_dir]
  python download_wfwf_async.py <list_url> --all [output_dir]
  python download_wfwf_async.py <list_url> --range 1-203 [output_dir]

The GPU is not useful here; the bottleneck is network I/O. We maximize throughput by:
- HTTP/2 multiplexing
- concurrent chapter-page fetches
- concurrent image downloads with a single shared connection pool
- file existence checks before writing to avoid duplicate work
"""

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from src.htmlutil import all_of, first, has_class, parse_html, stripped_text

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://wfwf504.com/",
}

# Tune these based on your bandwidth and CPU. More connections usually help up
# to the remote server's per-IP limit. The defaults are aggressive but safe.
IMAGE_CONCURRENCY = 32  # simultaneous image downloads
CHAPTER_PAGE_CONCURRENCY = 8  # simultaneous chapter HTML pages fetched
CHAPTER_DOWNLOAD_CONCURRENCY = 5  # chapters whose images are active at once


def sanitize_name(name: str) -> str:
    """Make a string safe for a folder/file name."""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


# ── Selectors, as XPath ─────────────────────────────────────────────────────

_XP_VIEWER_IMAGES = "//*[@id='vimg-area']//img"
_XP_CHAPTER_LINKS = f".//div[{has_class('list-sec')}]//a[{has_class('ep-item')}][@href]"
_XP_PAGE_TITLE = "//title"


def extract_image_urls(html: str, base_url: str) -> list[str]:
    """Return full image URLs from a viewer page, preferring data-src over src."""
    urls = []
    for img in all_of(parse_html(html), _XP_VIEWER_IMAGES):
        url = img.get("data-src") or img.get("src")
        if not url:
            continue
        full = urljoin(base_url, url)
        if "sprite.png" in full:
            continue
        urls.append(full)
    return urls


def extract_chapter_links(html: str, base_url: str) -> list[tuple[str, str, str]]:
    """Parse a list page and return [(full_url, num, title), ...]."""
    items = []
    for a in all_of(parse_html(html), _XP_CHAPTER_LINKS):
        href = a.get("href")
        if not href:
            continue
        num_el = first(a, f".//div[{has_class('ep-num')}]")
        num = a.get("data-num") or (stripped_text(num_el) if num_el is not None else "") or "0"
        title_el = first(a, f".//div[{has_class('ep-title')}]")
        title = stripped_text(title_el) if title_el is not None else ""
        items.append((urljoin(base_url, href), num, sanitize_name(title)))
    return items


def extract_pagination_urls(html: str, base_url: str) -> list[str]:
    """Return deduplicated absolute pagination URLs sorted by page number."""
    pages = {}
    for a in all_of(parse_html(html), f".//div[{has_class('pagi-wrap')}]//a[{has_class('pg-btn')}][@href]"):
        href = a.get("href")
        if not href or "pg=" not in href:
            continue
        full = urljoin(base_url, href)
        qs = parse_qs(urlparse(full).query)
        try:
            pages[int(qs["pg"][0])] = full
        except (KeyError, ValueError, IndexError):
            continue
    return [pages[k] for k in sorted(pages)]


async def fetch_text(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Fetch a URL and return (text, resolved final url)."""
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return resp.text, str(resp.url)


async def fetch_all_chapters(
    client: httpx.AsyncClient,
    list_url: str,
) -> list[tuple[str, str, str]]:
    """Discover every chapter link across all pagination pages."""
    first_html, resolved = await fetch_text(client, list_url)
    first_links = extract_chapter_links(first_html, resolved)
    page_urls = extract_pagination_urls(first_html, resolved)

    # Fetch remaining list pages concurrently.
    list_results = await asyncio.gather(*(fetch_text(client, u) for u in page_urls), return_exceptions=True)

    all_items = list(first_links)
    seen_nums = {num for _, num, _ in first_links}
    for res in list_results:
        if isinstance(res, BaseException):
            print(f"Warning: failed to fetch a list page: {res}")
            continue
        html, base = res
        for url, num, title in extract_chapter_links(html, base):
            if num not in seen_nums:
                seen_nums.add(num)
                all_items.append((url, num, title))

    # Newest-first order. int() here is safe: extract_chapter_links always
    # emits a numeric string (data-num, the ep-num text, or "0").
    return sorted(all_items, key=lambda x: int(x[1]), reverse=True)


async def download_image(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    index: int,
    total: int,
    semaphore: asyncio.Semaphore,
) -> None:
    """Download a single image to dest with bounded concurrency."""
    async with semaphore:
        if dest.exists():
            print(f"  [{index}/{total}] already exists: {dest.name}")
            return
        resp = await client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        print(f"  [{index}/{total}] downloaded {dest.name}")


async def download_chapter(
    client: httpx.AsyncClient,
    url: str,
    out_dir: Path,
    page_semaphore: asyncio.Semaphore,
    image_semaphore: asyncio.Semaphore,
    chapter_semaphore: asyncio.Semaphore,
) -> Path:
    """Download all images for one chapter URL into its own folder."""
    async with chapter_semaphore:
        async with page_semaphore:
            html, resolved = await fetch_text(client, url)
            title_el = first(parse_html(html), _XP_PAGE_TITLE)
            title = sanitize_name(stripped_text(title_el) if title_el is not None else "chapter")

            parsed = urlparse(resolved)
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()} if parsed.query else {}
            num = qs.get("num", "unknown")
            chapter_dir = out_dir / f"num{num}_{title}"
            chapter_dir.mkdir(parents=True, exist_ok=True)

            urls = extract_image_urls(html, resolved)
            print(f"Chapter {num}: {len(urls)} image(s) -> {chapter_dir.name}")

        await asyncio.gather(
            *(
                download_image(
                    client,
                    img_url,
                    chapter_dir / f"{i:04d}{Path(urlparse(img_url).path).suffix or '.jpg'}",
                    i,
                    len(urls),
                    image_semaphore,
                )
                for i, img_url in enumerate(urls, start=1)
            )
        )
    return chapter_dir


async def download_chapters(
    client: httpx.AsyncClient,
    chapters: list[tuple[str, str, str]],
    out_dir: Path,
) -> list[Path]:
    """Download many chapters concurrently with bounded chapter/page/image concurrency."""
    chapter_sem = asyncio.Semaphore(CHAPTER_DOWNLOAD_CONCURRENCY)
    page_sem = asyncio.Semaphore(CHAPTER_PAGE_CONCURRENCY)
    image_sem = asyncio.Semaphore(IMAGE_CONCURRENCY)

    return await asyncio.gather(
        *(download_chapter(client, url, out_dir, page_sem, image_sem, chapter_sem) for url, _, _ in chapters)
    )


async def download_series(
    list_url: str,
    out_dir: Path,
    *,
    start: int | None = None,
    end: int | None = None,
) -> list[Path]:
    """Download every (or a range of) chapter from a /list?toon=... URL."""
    limits = httpx.Limits(
        max_connections=IMAGE_CONCURRENCY,
        max_keepalive_connections=IMAGE_CONCURRENCY,
    )
    async with httpx.AsyncClient(
        headers=HEADERS,
        http2=True,
        limits=limits,
        timeout=httpx.Timeout(60.0, connect=10.0),
    ) as client:
        chapters = await fetch_all_chapters(client, list_url)
        if start is not None or end is not None:
            chapters = [
                (u, n, t)
                for u, n, t in chapters
                if (start is None or int(n) >= start) and (end is None or int(n) <= end)
            ]
        print(f"Downloading {len(chapters)} chapter(s) to {out_dir}")
        return await download_chapters(client, chapters, out_dir)


async def download_chapter_standalone(url: str, out_dir: Path | None = None) -> Path:
    """Download a single chapter with its own httpx client."""
    limits = httpx.Limits(
        max_connections=IMAGE_CONCURRENCY,
        max_keepalive_connections=IMAGE_CONCURRENCY,
    )
    async with httpx.AsyncClient(
        headers=HEADERS,
        http2=True,
        limits=limits,
        timeout=httpx.Timeout(60.0, connect=10.0),
    ) as client:
        return await download_chapter(
            client,
            url,
            out_dir or Path.cwd(),
            asyncio.Semaphore(1),
            asyncio.Semaphore(IMAGE_CONCURRENCY),
            asyncio.Semaphore(1),
        )


def parse_range_arg(arg: str) -> tuple[int | None, int | None]:
    """Convert '203', '1-203', '-203', or '1-' into (start, end)."""
    if "-" in arg:
        a, b = arg.split("-", 1)
        start = int(a) if a else None
        end = int(b) if b else None
        return start, end
    return int(arg), int(arg)


def main() -> None:
    if len(sys.argv) < 2:
        print(
            """Usage:
  python download_wfwf_async.py <chapter_url> [output_dir]
  python download_wfwf_async.py <list_url> --all [output_dir]
  python download_wfwf_async.py <list_url> --range 1-203 [output_dir]
"""
        )
        sys.exit(1)
    url = sys.argv[1]
    out = Path(sys.argv[-1]) if len(sys.argv) > 2 and not sys.argv[-1].startswith("--") else Path.cwd()

    if "/list?" in url:
        start = end = None
        if "--range" in sys.argv:
            idx = sys.argv.index("--range")
            start, end = parse_range_arg(sys.argv[idx + 1])
        elif "--all" not in sys.argv:
            print("For list URLs use --all or --range START-END")
            sys.exit(1)
        asyncio.run(download_series(url, out, start=start, end=end))
    else:
        chapter_dir = asyncio.run(download_chapter_standalone(url, out))
        print(f"Done. Saved to: {chapter_dir}")


if __name__ == "__main__":
    main()
