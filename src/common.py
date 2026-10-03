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

Resume is two-layered. Per-image, `download_image` treats a file as already
done not just at its source-URL extension but also at its `.jpg` sibling --
the post-run conversion pass (src/convert.py) renames every non-JPEG image
to `.jpg` and deletes the source, so a re-run's freshly-fetched listing
still points at e.g. `.webp` even though the file that's actually on disk
is `.jpg`; without this, every re-run of an already-converted series would
silently redownload everything. Per-chapter, `chapter_manifest.json` records
each chapter's image count once it completes, so a later run can confirm a
chapter's folder still matches on disk (same real decode check as any other
resume) and skip that chapter's listing-page fetch entirely instead of
hitting the network just to relearn a count it already knows.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import random
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from term import cerror, cinfo, cwarning, log_debug

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 60.0
IMAGE_CONCURRENCY = 24
IMAGE_CONCURRENCY_MIN = 4
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


def _jittered_delay(attempt: int, *, base: float = 0.5, cap: float = 5.0) -> float:
    """Backoff for a transient/integrity-check failure, with jitter.

    A fixed `base * (attempt + 1)` delay is fine for one image, but with
    IMAGE_CONCURRENCY images retrying in parallel, every one of them that
    failed on the same round sleeps for the same duration and then retries
    in the same instant -- a self-inflicted thundering herd against a CDN
    that may already be struggling. The random component spreads retries
    out instead of re-synchronizing them.
    """
    raw = min(base * (attempt + 1), cap)
    return raw + random.uniform(0, raw * 0.5)


class AdaptiveLimiter:
    """Concurrency limiter that backs off on failure and recovers on success.

    A plain semaphore holds concurrency at a fixed number picked in advance;
    that either leaves headroom unused on a fast CDN or keeps hammering a
    struggling one at the exact rate that's causing its drops/truncated
    responses in the first place ("downloaded file failed its integrity
    check"). This applies the same idea as TCP's AIMD congestion control:
    every failed attempt multiplicatively shrinks the allowed concurrency
    (down to `minimum`), every successful one nudges it back up by a small
    fixed step (up to `maximum`) -- so a run settles near whatever the
    remote server actually sustains instead of a hand-picked constant.
    """

    _DECREASE_FACTOR = 0.75
    _INCREASE_STEP = 0.5

    def __init__(self, initial: int, minimum: int, maximum: int) -> None:
        self._limit = float(initial)
        self._minimum = minimum
        self._maximum = maximum
        self._in_flight = 0
        self._condition = asyncio.Condition()

    @property
    def current_limit(self) -> int:
        return round(self._limit)

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight < self._limit)
            self._in_flight += 1

    async def release(self, *, success: bool) -> None:
        async with self._condition:
            self._in_flight -= 1
            before = round(self._limit)
            if success:
                self._limit = min(self._maximum, self._limit + self._INCREASE_STEP)
            else:
                self._limit = max(self._minimum, self._limit * self._DECREASE_FACTOR)
            if round(self._limit) != before:
                log_debug(f"adaptive concurrency limit {before} -> {round(self._limit)} (success={success})")
            self._condition.notify_all()


# A file shorter than the header sniffed below cannot be an image. Byte size
# says nothing more than that: a blank 720x2000 page is 102 bytes as lossless
# WebP (a blank sliver 38), smaller than a 1x1 "hotlink blocked" PNG (69).
# Placeholders are told apart by their decoded size instead (_MIN_PAGE_SIDE).
_MIN_IMAGE_BYTES = 16
# A decoded image is a page only when at least one side reaches this. Tracking
# pixels and hotlink placeholders (1x1, 2x2) fall below it in both. Requiring
# it of *both* sides would refuse a real page: a strip cut into fixed-height
# slices ends in a remainder that can be a few pixels tall, and a refused page
# leaves its chapter incomplete on every run.
_MIN_PAGE_SIDE = 16
_IMAGE_MAGIC = (b"\xff\xd8", b"RIFF", b"\x89PNG", b"GIF8", b"II*\x00", b"MM\x00*", b"BM")
# AVIF/HEIF start with an `ftyp` box whose 4-byte size varies by encoder (0x18, 0x1c, 0x20, ...).
_FTYP_AT = slice(4, 8)

# File extensions a page may be saved under, and the ones that name each
# format Pillow reports (the first is the canonical one). A page is saved
# under the extension of what it actually is: a URL's own suffix can be a
# script (`image.php?id=3`), missing, or wrong (a CDN answering a `.jpg`
# URL with WebP), and a file named for the wrong format is skipped by the
# JPEG pass and by OmniScan's importer.
IMAGE_EXTS = (".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff")
# `.jfif` is found on resume but never written: OmniScan's importer does not list it as an image.
_FORMAT_EXTS: dict[str, tuple[str, ...]] = {
    "JPEG": (".jpg", ".jpeg"),
    "MPO": (".jpg", ".jpeg"),  # multi-picture JPEG, e.g. from a phone camera
    "PNG": (".png",),
    "WEBP": (".webp",),
    "GIF": (".gif",),
    "AVIF": (".avif",),
    "BMP": (".bmp",),
    "TIFF": (".tif", ".tiff"),
}


# The longest file name a page goes through: `0001.jpeg.part` (convert.py's
# `0001.jpg.part` is shorter), plus the separator before it.
_LONGEST_PAGE_NAME = len("\\0001") + max(len(ext) for ext in IMAGE_EXTS) + len(".part")


@functools.cache
def _windows_path_limit() -> int | None:
    """The longest path this process can write: 259 on Windows without long path support, else None.

    Long paths are off on a default Windows install (the LongPathsEnabled
    policy); Python itself is built to use them once that is switched on.
    """
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            enabled, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    except OSError:
        enabled = 0
    return None if enabled == 1 else 259


def _check_path_length(folder: Path) -> None:
    """Refuse a chapter folder whose pages Windows could not write, before any download starts.

    Unchecked, the folder itself may still fit while every page in it fails
    on each of its retries and the chapter comes back incomplete on every run.
    """
    limit = _windows_path_limit()
    if limit is None:
        return
    length = len(str(folder.absolute())) + _LONGEST_PAGE_NAME
    if length > limit:
        raise OSError(
            f"the paths in {folder} would be {length} characters, over Windows' limit of {limit}: "
            'turn on long paths (README "Long paths on Windows") or choose a shorter --out'
        )


def page_suffix(url: str) -> str:
    """The extension a page from `url` is first expected under: the URL's own when it names an image, else .jpg."""
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTS else ".jpg"


def _suffix_for(fmt: str, expected: str) -> str:
    """The extension to save a page of Pillow format `fmt` under; `expected` when it already names that format."""
    names = _FORMAT_EXTS.get(fmt)
    if names is None:
        return expected
    return expected if expected in names else names[0]


def _decoded_format_sync(path: Path) -> str | None:
    """The image format of `path` when it decodes completely into a page, else None.

    A real decode check, not just a magic-byte sniff: a dropped HTTP/2
    stream or an overloaded CDN can answer a "200 OK" with a body that
    starts correctly but is truncated mid-image; that passes a magic-byte
    check and would otherwise be accepted as a complete download. A full
    `.load()` forces Pillow to decode every scanline, so a truncated JPEG --
    the common shape of a cut-off stream -- raises instead of silently
    succeeding the way the cheaper `.verify()` sometimes does. Pillow's
    default (LOAD_TRUNCATED_IMAGES = False) is what makes that raise; this
    never overrides it. An image that decodes but is tiny in both directions
    is a server's placeholder, not a page (see _MIN_PAGE_SIDE).
    """
    try:
        with Image.open(path) as img:
            img.load()
            if max(img.size) < _MIN_PAGE_SIDE:
                return None
            return img.format or ""
    except (OSError, UnidentifiedImageError, ValueError):
        return None


async def _image_format(path: Path) -> str | None:
    """The format of a complete, structurally valid image at `path`, else None.

    The cheap checks (existence, size, magic bytes) run first so a missing
    file -- the common case for a fresh download -- never pays for a decode;
    only a file that clears them goes through the thread-offloaded decode so
    the event loop doesn't block while other downloads are in flight.
    """
    try:
        if path.stat().st_size < _MIN_IMAGE_BYTES:
            return None
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if not (head.startswith(_IMAGE_MAGIC) or head[_FTYP_AT] == b"ftyp"):
        return None
    return await asyncio.to_thread(_decoded_format_sync, path)


async def _any_plausible(paths: list[Path]) -> bool:
    """True when one of `paths` passes _plausible_download (checked in order, stopping at the first)."""
    for path in paths:
        if await _plausible_download(path):
            return True
    return False


async def _plausible_download(path: Path) -> bool:
    """True when an existing file is a complete, structurally valid image.

    Used both for resume (is a file already on disk from a previous run
    actually good, or should it be re-downloaded?) and to validate a
    freshly-written file before it's accepted (see _image_format).
    """
    return await _image_format(path) is not None


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


# Windows refuses to replace or delete a file that another process holds open
# without delete sharing (Python's own open(), many editors, an antivirus
# scan): PermissionError, WinError 5 or 32. Such a hold is usually brief, so
# the bookkeeping writes wait it out for about 1.5 s before giving up.
_LOCKED_FILE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def _retry_if_locked(action: Callable[[], object]) -> None:
    """Run `action`, retrying while the file it replaces or deletes is held open elsewhere.

    Blocks with time.sleep: it only runs once every chapter of the run has
    finished, so there is nothing on the event loop left to hold up.
    """
    for delay in _LOCKED_FILE_RETRY_DELAYS:
        try:
            action()
            return
        except PermissionError:
            time.sleep(delay)
    action()


def _write_json_atomic(path: Path, data: object) -> None:
    """Write `data` as JSON to `path` through a temp file and a rename.

    A crash or a full disk mid-write then leaves the previous file intact
    instead of a truncated one -- which matters most for
    incomplete_chapters.json: an unreadable report reads as "nothing is
    incomplete", to this engine and to OmniScan's importer alike.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _retry_if_locked(lambda: tmp.replace(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
        _write_json_atomic(path, {"chapters": sorted(entries.values(), key=lambda e: e["folder"])})
    elif path.exists():
        _retry_if_locked(path.unlink)


def _load_manifest(out_dir: Path) -> dict[str, int]:
    """folder name -> image count, for chapters known complete as of the last run."""
    path = out_dir / "chapter_manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(folder): int(count) for folder, count in data.get("chapters", {}).items()}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}


def _write_manifest(out_dir: Path, results: list[ChapterResult]) -> None:
    """Merge this run's outcome into out_dir/chapter_manifest.json.

    Same merge-not-overwrite shape as _write_incomplete_report, for the same
    reason: out_dir can carry results from many separate runs. A chapter
    that went incomplete this run has its entry dropped rather than left
    stale, so a later run can't mistake a half-finished chapter for one the
    manifest fast path is allowed to trust.
    """
    path = out_dir / "chapter_manifest.json"
    entries: dict[str, int] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            entries = {str(k): int(v) for k, v in existing.get("chapters", {}).items()}
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            entries = {}

    for r in results:
        if r.complete and r.total > 0:
            entries[r.folder] = r.total
        else:
            entries.pop(r.folder, None)

    if entries:
        _write_json_atomic(path, {"chapters": dict(sorted(entries.items()))})
    elif path.exists():
        _retry_if_locked(path.unlink)


async def _verify_folder_matches_manifest(folder: Path, expected_count: int) -> bool:
    """True when `folder` already holds exactly the manifest's image count.

    Requires the exact sequential stems (0001..expected_count), not just a
    matching file count, so e.g. a leftover duplicate alongside one missing
    page doesn't accidentally pass. Every file still gets the same real
    decode check as a fresh download (_plausible_download) -- this only
    saves the listing-page network round trip, never the integrity check.
    """
    if not folder.is_dir():
        return False
    files = [p for p in folder.iterdir() if p.is_file() and p.stem.isdigit()]
    expected_stems = {f"{i:04d}" for i in range(1, expected_count + 1)}
    if len(files) != expected_count or {p.stem for p in files} != expected_stems:
        return False
    checks = await asyncio.gather(*(_plausible_download(p) for p in files))
    return all(checks)


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

    async def download_image(client: httpx.AsyncClient, url: str, dest: Path, image_sem: AdaptiveLimiter) -> bool:
        # CDNs occasionally drop HTTP/2 streams under load, or answer a "200"
        # with a truncated/garbage body -- both look identical to a caller
        # that only checks the status code. Every attempt writes to a `.part`
        # file first and only renames it into place once `_plausible_download`
        # confirms it's a complete, valid image: `dest` therefore never exists
        # in a corrupt state, and a bad response is retried like any other
        # failure instead of being silently accepted. 429/503 get their own,
        # longer backoff (honoring Retry-After) since those mean "back off",
        # not "broken".
        #
        # The concurrency slot is held only for the request itself, not for
        # the backoff sleep afterwards -- holding it through the sleep (the
        # previous behavior) ties up one of a handful of global slots doing
        # nothing while other images that could make progress wait behind
        # it. `image_sem` also gets told whether this attempt succeeded so
        # it can shrink/grow the allowed concurrency (see AdaptiveLimiter).
        #
        # Resume looks at every image extension of the page's stem, not just
        # `dest`'s: the post-run conversion (src/convert.py) renames each
        # non-JPEG page to `.jpg`, and a page is saved under the extension
        # of its real format, which a URL can name wrongly or not at all --
        # so the file a previous run left can sit at a different extension
        # than this run's (freshly refetched) URL suggests. Checking them all
        # is what makes resume skip finished pages instead of redownloading.
        candidates = [dest, *(dest.with_suffix(ext) for ext in IMAGE_EXTS if ext != dest.suffix)]
        tmp = dest.with_suffix(dest.suffix + ".part")
        last_exc: Exception | None = None
        for attempt in range(MAX_IMAGE_ATTEMPTS):
            await image_sem.acquire()
            ok = False
            delay = 0.0
            started = time.perf_counter()
            try:
                if await _any_plausible(candidates):
                    ok = True
                else:
                    async with client.stream("GET", url, timeout=120) as resp:
                        resp.raise_for_status()
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with tmp.open("wb") as out:
                            async for chunk in resp.aiter_bytes(64 * 1024):
                                out.write(chunk)
                    fmt = await _image_format(tmp)
                    if fmt is not None:
                        final = dest.with_suffix(_suffix_for(fmt, dest.suffix))
                        tmp.replace(final)
                        for stale in candidates:  # none passed the check above: leftovers of a broken earlier try
                            if stale != final:
                                stale.unlink(missing_ok=True)
                        ok = True
                    else:
                        last_exc = ValueError("downloaded file failed its integrity check")
                        delay = _jittered_delay(attempt)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                delay = retry_delay(exc.response, attempt) if status in (429, 503) else _jittered_delay(attempt)
            except (httpx.HTTPError, OSError) as exc:
                last_exc = exc
                delay = _jittered_delay(attempt)
            finally:
                if not ok:
                    # A stream dropped mid-body leaves a half-written .part behind;
                    # the next attempt overwrites it, but the last one must not
                    # leave it in the chapter folder.
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
                elapsed_ms = (time.perf_counter() - started) * 1000
                log_debug(
                    f"{url} attempt={attempt + 1}/{MAX_IMAGE_ATTEMPTS} ok={ok} "
                    f"elapsed_ms={elapsed_ms:.0f} concurrency_limit={image_sem.current_limit}"
                )
                await image_sem.release(success=ok)
            if ok:
                return True
            if delay:
                await asyncio.sleep(delay)
        cwarning(f"      image failed {url}: {last_exc}")
        return False

    async def download_chapter(
        client: httpx.AsyncClient,
        chapter_url: str,
        out_dir: Path,
        chapter_sem: asyncio.Semaphore,
        image_sem: AdaptiveLimiter,
        manifest: dict[str, int],
        dry_run: bool = False,
    ) -> ChapterResult:
        async with chapter_sem:
            folder_name = clean_name(chapter_folder_name(chapter_url))

            expected = manifest.get(folder_name)
            if (
                expected is not None
                and not dry_run
                and await _verify_folder_matches_manifest(out_dir / folder_name, expected)
            ):
                log_debug(f"{folder_name}: {expected} image(s) verified on disk, skipped listing fetch")
                return ChapterResult(folder_name, chapter_url, ok=expected, total=expected, complete=True)

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
            _check_path_length(folder)
            folder.mkdir(parents=True, exist_ok=True)

            def dest_for(idx: int) -> Path:
                return folder / f"{idx:04d}{page_suffix(image_urls[idx - 1])}"

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
                    await asyncio.sleep(CHAPTER_RETRY_PAUSE + random.uniform(0, 2.0))
                results = await asyncio.gather(*(attempt_one(i) for i in pending), return_exceptions=True)
                for idx, result in zip(pending, results, strict=True):
                    if isinstance(result, BaseException):  # download_image handles the expected ones itself
                        log_debug(f"{folder_name} image {idx}: unexpected {type(result).__name__}: {result}")
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

    def _one_url_per_folder(chapter_urls: list[str]) -> list[str]:
        """The chapter URLs without those whose folder an earlier one already claims.

        Two listing entries that name the same folder (the same chapter
        linked twice under different URLs, or two chapters a site gives one
        number) would otherwise download into one folder at once, writing
        and replacing each other's pages and leaving a mix of both.
        """
        claimed: dict[str, str] = {}
        kept: list[str] = []
        for url in chapter_urls:
            try:
                folder = clean_name(chapter_folder_name(url))
            except Exception:  # noqa: BLE001 - download_chapter reports it as that chapter's failure
                kept.append(url)
                continue
            if folder in claimed:
                cwarning(f"  skipped {url}: its folder {folder} is already {claimed[folder]}'s")
                continue
            claimed[folder] = url
            kept.append(url)
        return kept

    async def download_series(
        client: httpx.AsyncClient,
        chapter_urls: list[str],
        out_dir: Path,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        image_sem = AdaptiveLimiter(IMAGE_CONCURRENCY, IMAGE_CONCURRENCY_MIN, IMAGE_CONCURRENCY)
        chapter_sem = asyncio.Semaphore(CHAPTER_DOWNLOAD_CONCURRENCY)
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = {} if dry_run else _load_manifest(out_dir)

        urls = _one_url_per_folder(chapter_urls)
        gathered = await asyncio.gather(
            *(download_chapter(client, url, out_dir, chapter_sem, image_sem, manifest, dry_run) for url in urls),
            return_exceptions=True,
        )
        total_ok_images = 0
        complete_chapters = 0
        incomplete: list[ChapterResult] = []
        failures = 0
        results: list[ChapterResult] = []
        for url, result in zip(urls, gathered, strict=True):
            if isinstance(result, BaseException):
                # Name the chapter: the exception alone ("Expecting value: line 1
                # column 1", "'data'") does not say which one to look at.
                cerror(f"Chapter failed: {url} ({type(result).__name__}: {result})")
                failures += 1
                continue
            results.append(result)
            total_ok_images += result.ok
            if result.complete:
                complete_chapters += 1
            else:
                incomplete.append(result)

        bookkeeping_errors: list[str] = []
        if not dry_run:
            for write in (_write_incomplete_report, _write_manifest):
                try:
                    write(out_dir, results)
                except OSError as exc:
                    # Every page is on disk by now: a bookkeeping file that stays
                    # locked must not throw away the run's result with it.
                    cwarning(f"Could not update the bookkeeping in {out_dir}: {exc}")
                    bookkeeping_errors.append(str(exc))

        stats: dict[str, Any] = {
            "chapters": complete_chapters,
            "images": total_ok_images,
            "failed_chapters": failures,
            "complete_chapters": [r.folder for r in results if r.complete],
            "incomplete_chapters": [r.folder for r in incomplete],
        }
        if bookkeeping_errors:
            stats["bookkeeping_error"] = "; ".join(bookkeeping_errors)
        return stats

    return download_series
