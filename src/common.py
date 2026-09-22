"""Shared helpers for site drivers.

Contains the pieces every site driver needs: the async HTTP client factory,
concurrency limits, filename sanitizing, and a reusable concurrent chapter
download engine driven by site-specific "get image urls" callables.

Reliability model (see AGENTS.md for the wider picture): a chapter is never
reported as done unless every one of its images is present *and* passes a
real decode check, not just a magic-byte sniff. A dropped stream, a 429, or
a CDN that serves a "200 OK" with a truncated body are all treated as the
same kind of failure and retried -- first per-image (download_image), then
per-chapter (download_chapter re-attempts whatever is still missing after
the per-image retries are exhausted). Anything still missing after that is
never silently accepted: it's reported by name in the returned stats and
recorded in `incomplete_chapters.json` under the output directory, so a
later re-run (or a human) can find and finish it -- the file is merged, not
overwritten, so it stays accurate across multiple runs of the same series.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from term import cerror, cinfo, cwarning

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 60.0
IMAGE_CONCURRENCY = 24
CHAPTER_PAGE_CONCURRENCY = 6
CHAPTER_DOWNLOAD_CONCURRENCY = 5

# Per-image attempts (covers transport errors, HTTP errors, and a corrupt or
# truncated body), chapter-level retry rounds for whatever is still missing
# once every image has had its individual attempts, and the pause between
# those rounds -- long enough to matter against sustained rate-limiting,
# short enough not to stall a large series over one flaky chapter.
MAX_IMAGE_ATTEMPTS = 6
CHAPTER_RETRY_ROUNDS = 3
CHAPTER_RETRY_PAUSE = 5.0
LISTING_RETRY_ATTEMPTS = 3
LISTING_RETRY_BASE_DELAY = 2.0

ILLEGAL_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Manga strips can legitimately be very tall; don't let Pillow's decompression
# -bomb guard (tuned for photos) reject a real chapter page.
Image.MAX_IMAGE_PIXELS = None


def clean_name(text: str | None) -> str:
    """Sanitize a string for use in folder/file names on Windows.

    This is also the security boundary against path traversal: every
    driver-supplied name that becomes part of a filesystem path (series
    slug, chapter folder) must go through this, because it strips `/` and
    `\\` unconditionally. A slug of ".." or "../../etc" collapses to
    "untitled"/"etc" rather than escaping the downloads tree -- see
    SiteDriver.series_folder and download_chapter below, which are the two
    places that join a driver's output onto a real path.
    """
    if not text:
        return "untitled"
    cleaned = ILLEGAL_NAME_RE.sub("", str(text)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(". ")
    return cleaned[:180] or "untitled"


def limits() -> httpx.Limits:
    return httpx.Limits(max_keepalive_connections=20, max_connections=100)


def retry_delay(response: httpx.Response, attempt: int) -> float:
    """Backoff for a 429/503 response: honor Retry-After, else exponential."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(float(retry_after), 1.0)
        except ValueError:
            pass
    return min(2.0 * (attempt + 1), 15.0)


# Minimum plausible image: magic bytes present and not absurdly small.
_MIN_IMAGE_BYTES = 512
_IMAGE_MAGIC = (b"\xff\xd8", b"RIFF", b"\x89PNG", b"GIF8", b"II*\x00", b"MM\x00*", b"\x00\x00\x00 ftyp")


def _verify_image_sync(path: Path) -> bool:
    """Real decode check, not just a magic-byte sniff.

    A dropped HTTP/2 stream or an overloaded CDN can answer a "200 OK" with
    a body that starts correctly but is truncated mid-image; that passes a
    magic-byte check and would otherwise be accepted as a complete download.
    A full `.load()` forces Pillow to decode every scanline, so a truncated
    JPEG -- the common shape of a cut-off stream -- raises instead of
    silently succeeding the way the cheaper `.verify()` sometimes does.
    Pillow's default (LOAD_TRUNCATED_IMAGES = False) is what makes that
    raise; this never overrides it.
    """
    try:
        with Image.open(path) as img:
            img.load()
        return True
    except (OSError, UnidentifiedImageError, ValueError):
        return False


async def _plausible_download(path: Path) -> bool:
    """True when an existing file is a complete, structurally valid image.

    Used both for resume (is a file already on disk from a previous run
    actually good, or should it be re-downloaded?) and to validate a
    freshly-written file before it's accepted. The cheap checks (existence,
    size, magic bytes) run first so a missing file -- the common case for a
    fresh download -- never pays for a decode; only a file that clears both
    goes through the thread-offloaded PIL verify so the event loop doesn't
    block while other downloads are in flight.
    """
    try:
        if path.stat().st_size < _MIN_IMAGE_BYTES:
            return False
        with path.open("rb") as f:
            if not f.read(16).startswith(_IMAGE_MAGIC):
                return False
    except OSError:
        return False
    return await asyncio.to_thread(_verify_image_sync, path)


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


@dataclass
class ChapterResult:
    folder: str
    chapter_url: str
    ok: int
    total: int
    complete: bool


def _write_incomplete_report(out_dir: Path, results: list[ChapterResult]) -> None:
    """Merge this run's outcome into out_dir/incomplete_chapters.json.

    Merged, not overwritten: out_dir (e.g. a single_chapters folder) can
    accumulate results from many separate runs, so a chapter that isn't part
    of *this* run must not be dropped from the report, and a chapter that
    *did* complete this time must be cleared even if a previous run left it
    listed. The end state always reflects current truth on disk.
    """
    path = out_dir / "incomplete_chapters.json"
    entries: dict[str, dict[str, Any]] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            entries = {e["folder"]: e for e in existing.get("chapters", [])}
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            entries = {}

    now = datetime.now(UTC).isoformat()
    for r in results:
        if r.complete:
            entries.pop(r.folder, None)
        else:
            entries[r.folder] = {
                "folder": r.folder,
                "chapter_url": r.chapter_url,
                "downloaded": r.ok,
                "total": r.total,
                "missing": r.total - r.ok,
                "last_attempt": now,
            }

    if entries:
        path.write_text(
            json.dumps({"chapters": sorted(entries.values(), key=lambda e: e["folder"])}, indent=2),
            encoding="utf-8",
        )
    elif path.exists():
        path.unlink()


def make_downloader(
    *,
    site_label: str,
    fetch_image_urls: Callable[[httpx.AsyncClient, str], Awaitable[list[str]]],
    chapter_folder_name: Callable[[str], str],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build a download_series function from site-specific pieces.

    fetch_image_urls(client, chapter_url) -> ordered image urls for one chapter
    chapter_folder_name(chapter_url) -> folder name for that chapter
    """

    async def download_image(client: httpx.AsyncClient, url: str, dest: Path, image_sem: asyncio.Semaphore) -> bool:
        # CDNs occasionally drop HTTP/2 streams under load, or answer a "200"
        # with a truncated/garbage body -- both look identical to a caller
        # that only checks the status code. Every attempt writes to a `.part`
        # file first and only renames it into place once `_plausible_download`
        # confirms it's a complete, valid image: `dest` therefore never exists
        # in a corrupt state, and a bad response is retried like any other
        # failure instead of being silently accepted. 429/503 get their own,
        # longer backoff (honoring Retry-After) since those mean "back off",
        # not "broken".
        last_exc: Exception | None = None
        for attempt in range(MAX_IMAGE_ATTEMPTS):
            try:
                async with image_sem:
                    if await _plausible_download(dest):
                        return True
                    async with client.stream("GET", url, timeout=120) as resp:
                        resp.raise_for_status()
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        tmp = dest.with_suffix(dest.suffix + ".part")
                        with tmp.open("wb") as out:
                            async for chunk in resp.aiter_bytes(64 * 1024):
                                out.write(chunk)
                    if not await _plausible_download(tmp):
                        tmp.unlink(missing_ok=True)
                        last_exc = ValueError("downloaded file failed its integrity check")
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    tmp.replace(dest)
                    return True
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                delay = retry_delay(exc.response, attempt) if status in (429, 503) else 0.5 * (attempt + 1)
                await asyncio.sleep(delay)
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
    ) -> ChapterResult:
        async with chapter_sem:
            folder_name = clean_name(chapter_folder_name(chapter_url))

            image_urls: list[str] = []
            last_exc: Exception | None = None
            for attempt in range(LISTING_RETRY_ATTEMPTS):
                try:
                    image_urls = await fetch_image_urls(client, chapter_url)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    image_urls = []
                if image_urls:
                    break
                if attempt < LISTING_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(LISTING_RETRY_BASE_DELAY * (attempt + 1))

            if not image_urls:
                if last_exc is not None:
                    cwarning(f"  failed to read {folder_name}: {last_exc}")
                else:
                    cwarning(f"  no images in {folder_name} ({chapter_url})")
                return ChapterResult(folder_name, chapter_url, ok=0, total=0, complete=False)

            if dry_run:
                cinfo(f"  [dry-run] {folder_name}: {len(image_urls)} images")
                return ChapterResult(folder_name, chapter_url, ok=len(image_urls), total=len(image_urls), complete=True)

            cinfo(f"  downloading {folder_name}: {len(image_urls)} images")
            folder = out_dir / folder_name
            folder.mkdir(parents=True, exist_ok=True)

            def dest_for(idx: int) -> Path:
                ext = Path(urlparse(image_urls[idx - 1]).path).suffix.lower() or ".jpg"
                return folder / f"{idx:04d}{ext}"

            async def attempt_one(idx: int) -> bool:
                return await download_image(client, image_urls[idx - 1], dest_for(idx), image_sem)

            pending = list(range(1, len(image_urls) + 1))
            ok_count = 0
            for round_num in range(CHAPTER_RETRY_ROUNDS):
                if round_num > 0:
                    cwarning(
                        f"    retrying {len(pending)} image(s) in {folder_name} "
                        f"(attempt {round_num + 1}/{CHAPTER_RETRY_ROUNDS})"
                    )
                    await asyncio.sleep(CHAPTER_RETRY_PAUSE)
                results = await asyncio.gather(*(attempt_one(i) for i in pending), return_exceptions=True)
                still_pending = [idx for idx, result in zip(pending, results, strict=True) if result is not True]
                ok_count += len(pending) - len(still_pending)
                pending = still_pending
                if not pending:
                    break

            complete = not pending
            if not complete:
                cerror(
                    f"    INCOMPLETE: {folder_name} is missing {len(pending)}/{len(image_urls)} "
                    f"image(s) after {CHAPTER_RETRY_ROUNDS} attempts"
                )
            return ChapterResult(folder_name, chapter_url, ok=ok_count, total=len(image_urls), complete=complete)

    async def download_series(
        client: httpx.AsyncClient,
        chapter_urls: list[str],
        out_dir: Path,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        image_sem = asyncio.Semaphore(IMAGE_CONCURRENCY)
        chapter_sem = asyncio.Semaphore(CHAPTER_DOWNLOAD_CONCURRENCY)
        out_dir.mkdir(parents=True, exist_ok=True)

        gathered = await asyncio.gather(
            *(download_chapter(client, url, out_dir, chapter_sem, image_sem, dry_run) for url in chapter_urls),
            return_exceptions=True,
        )
        total_ok_images = 0
        complete_chapters = 0
        incomplete: list[ChapterResult] = []
        failures = 0
        results: list[ChapterResult] = []
        for result in gathered:
            if isinstance(result, BaseException):
                cerror(f"Chapter failed: {result}")
                failures += 1
                continue
            results.append(result)
            total_ok_images += result.ok
            if result.complete:
                complete_chapters += 1
            else:
                incomplete.append(result)

        if not dry_run:
            _write_incomplete_report(out_dir, results)

        return {
            "chapters": complete_chapters,
            "images": total_ok_images,
            "failed_chapters": failures,
            "incomplete_chapters": [r.folder for r in incomplete],
        }

    return download_series
