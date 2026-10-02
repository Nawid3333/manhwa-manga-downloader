"""Tests for src/common.py: filename sanitizing (incl. the path-traversal
guard), resume validation, the 429-aware retry backoff, and the concurrent
download engine's reliability guarantees -- per-image corruption retry,
chapter-level retry rounds, listing retry, and the incomplete-chapters.json
report -- via httpx.MockTransport, no real network calls."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import src.common as common

# Magic bytes + padding, but not a real image: used to test that a corrupt-
# but-magic-byte-plausible body is caught and retried, not silently accepted.
FAKE_JPEG_BYTES = b"\xff\xd8" + b"0" * 600


def _make_fetch(count: int, ext: str = ".jpg"):
    async def _fetch(_client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        return [f"{chapter_url}/{i}{ext}" for i in range(count)]

    return _fetch


# ---- clean_name / path-traversal guard --------------------------------


def test_clean_name_strips_illegal_characters():
    assert common.clean_name('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"


def test_clean_name_collapses_whitespace_and_trims_dots():
    assert common.clean_name("  Chapter   1.  ") == "Chapter 1"


def test_clean_name_falls_back_to_untitled():
    assert common.clean_name(None) == "untitled"
    assert common.clean_name("   ") == "untitled"


def test_clean_name_truncates_to_180_chars():
    assert len(common.clean_name("x" * 500)) == 180


def test_clean_name_neutralizes_bare_parent_traversal():
    assert common.clean_name("..") == "untitled"


def test_clean_name_neutralizes_embedded_traversal():
    assert common.clean_name("../../../escape") == "escape"


# ---- client() ------------------------------------------------------------


def test_client_sets_default_headers():
    c = common.client("https://example.com", referer="https://example.com/", accept="text/html")
    assert c.headers["user-agent"] == common.USER_AGENT
    assert c.headers["referer"] == "https://example.com/"
    assert c.headers["accept"] == "text/html"


# ---- retry_delay -----------------------------------------------------------


def test_retry_delay_honors_retry_after_header():
    resp = httpx.Response(429, headers={"Retry-After": "7"})
    assert common.retry_delay(resp, attempt=0) == 7.0


def test_retry_delay_falls_back_when_retry_after_missing():
    resp = httpx.Response(429)
    assert common.retry_delay(resp, attempt=0) == 2.0
    assert common.retry_delay(resp, attempt=2) == 6.0


def test_retry_delay_caps_exponential_backoff():
    resp = httpx.Response(429)
    assert common.retry_delay(resp, attempt=50) == 15.0


def test_retry_delay_ignores_unparseable_retry_after():
    resp = httpx.Response(429, headers={"Retry-After": "not-a-number"})
    assert common.retry_delay(resp, attempt=0) == 2.0


# ---- _plausible_download: real decode check, not just magic bytes ----------


async def test_plausible_download_missing_file(tmp_path: Path):
    assert await common._plausible_download(tmp_path / "missing.jpg") is False


async def test_plausible_download_too_small(tmp_path: Path):
    path = tmp_path / "tiny.jpg"
    path.write_bytes(b"\xff\xd8\x00")
    assert await common._plausible_download(path) is False


async def test_plausible_download_magic_bytes_but_not_a_real_image(tmp_path: Path):
    """The exact failure mode this check exists for: a truncated/garbage
    body that happens to start with the right magic bytes."""
    path = tmp_path / "junk.jpg"
    path.write_bytes(FAKE_JPEG_BYTES)
    assert await common._plausible_download(path) is False


async def test_plausible_download_valid_jpeg(tmp_path: Path, jpeg_bytes: bytes):
    path = tmp_path / "ok.jpg"
    path.write_bytes(jpeg_bytes)
    assert await common._plausible_download(path) is True


# ---- AdaptiveLimiter: AIMD concurrency control ------------------------------


async def test_adaptive_limiter_shrinks_on_failure_and_clamps_to_minimum():
    limiter = common.AdaptiveLimiter(initial=10, minimum=2, maximum=10)
    for _ in range(20):
        await limiter.acquire()
        await limiter.release(success=False)
    assert limiter.current_limit == 2


async def test_adaptive_limiter_grows_on_success_and_clamps_to_maximum():
    limiter = common.AdaptiveLimiter(initial=2, minimum=2, maximum=10)
    for _ in range(50):
        await limiter.acquire()
        await limiter.release(success=True)
    assert limiter.current_limit == 10


async def test_adaptive_limiter_recovers_after_a_failure_streak_ends():
    limiter = common.AdaptiveLimiter(initial=10, minimum=2, maximum=10)
    for _ in range(5):
        await limiter.acquire()
        await limiter.release(success=False)
    shrunk = limiter.current_limit
    assert shrunk < 10

    await limiter.acquire()
    await limiter.release(success=True)
    assert limiter.current_limit > shrunk


# ---- make_downloader / download_series: happy path --------------------------


async def test_download_series_writes_images(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(2),
        chapter_folder_name=lambda url: url.rsplit("/", 1)[-1],
    )

    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats == {
        "chapters": 1,
        "images": 2,
        "failed_chapters": 0,
        "complete_chapters": ["ch1"],
        "incomplete_chapters": [],
    }
    assert (tmp_path / "ch1" / "0001.jpg").exists()
    assert (tmp_path / "ch1" / "0002.jpg").exists()
    assert not (tmp_path / "ch1" / "incomplete_chapters.json").exists()
    assert not (tmp_path / "incomplete_chapters.json").exists()


class _DroppedStream(httpx.AsyncByteStream):
    """A body that starts like a JPEG and then dies: the shape of a dropped HTTP/2 stream."""

    async def __aiter__(self):
        yield b"\xff\xd8" + b"\x00" * 2048
        raise httpx.ReadError("connection dropped")


async def test_download_series_leaves_no_part_file_after_a_dropped_stream(tmp_path: Path, mock_client):
    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(1),
        chapter_folder_name=lambda url: url.rsplit("/", 1)[-1],
    )
    async with mock_client(lambda r: httpx.Response(200, stream=_DroppedStream())) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats["incomplete_chapters"] == ["ch1"]
    assert list((tmp_path / "ch1").iterdir()) == []


async def test_download_series_skips_existing_plausible_file(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=jpeg_bytes)

    chapter_dir = tmp_path / "ch1"
    chapter_dir.mkdir()
    (chapter_dir / "0001.jpg").write_bytes(jpeg_bytes)

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(2),
        chapter_folder_name=lambda url: url.rsplit("/", 1)[-1],
    )
    async with mock_client(handler) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats["images"] == 2
    # only the missing second image should have hit the network
    assert calls == ["https://fake.test/ch1/1.jpg"]


async def test_download_series_resume_recognizes_an_already_converted_jpg(
    tmp_path: Path, mock_client, jpeg_bytes: bytes
):
    """A previous run's post-download JPEG conversion (src/convert.py) deletes
    the original and leaves only the `.jpg`. A re-run's freshly-fetched
    listing still points at the original extension (e.g. `.webp`); resume
    must still recognize the file as already done via its `.jpg` sibling
    instead of redownloading it."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not hit the network for an already-converted image")

    chapter_dir = tmp_path / "ch1"
    chapter_dir.mkdir()
    (chapter_dir / "0001.jpg").write_bytes(jpeg_bytes)

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(1, ext=".webp"),
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(handler) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats == {
        "chapters": 1,
        "images": 1,
        "failed_chapters": 0,
        "complete_chapters": ["ch1"],
        "incomplete_chapters": [],
    }


async def test_download_series_sanitizes_a_malicious_folder_name(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    """chapter_folder_name is driver-supplied; a driver bug (or a hostile
    site feeding a driver bad data) must not be able to escape out_dir."""
    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(1),
        chapter_folder_name=lambda url: "../../escape",
    )
    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats["images"] == 1
    assert (tmp_path / "escape" / "0001.jpg").exists()
    assert not (tmp_path.parent / "escape").exists()


# ---- per-image retry: transient errors, 429, and corrupt data --------------


async def test_download_series_retries_after_429(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, content=jpeg_bytes)

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(1),
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(handler) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats["images"] == 1
    assert attempts["n"] == 3
    assert (tmp_path / "ch1" / "0001.jpg").exists()


async def test_download_series_retries_corrupt_body_instead_of_accepting_it(
    tmp_path: Path, mock_client, jpeg_bytes: bytes
):
    """A "200 OK" with a truncated/garbage body used to be silently accepted
    as a successful download. It must now be retried like any other failure."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(200, content=FAKE_JPEG_BYTES)
        return httpx.Response(200, content=jpeg_bytes)

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(1),
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(handler) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert attempts["n"] == 2
    assert stats == {
        "chapters": 1,
        "images": 1,
        "failed_chapters": 0,
        "complete_chapters": ["ch1"],
        "incomplete_chapters": [],
    }
    dest = tmp_path / "ch1" / "0001.jpg"
    assert dest.exists()
    assert await common._plausible_download(dest) is True
    assert not dest.with_suffix(".jpg.part").exists()


async def test_download_series_never_leaves_a_corrupt_file_at_dest(tmp_path: Path, mock_client):
    """If every attempt returns corrupt data, dest must never exist -- not
    even the bad version -- so a later resume can't mistake it for good."""
    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(1),
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(200, content=FAKE_JPEG_BYTES)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats["images"] == 0
    assert not (tmp_path / "ch1" / "0001.jpg").exists()


# ---- chapter-level retry rounds ---------------------------------------------


async def test_download_chapter_recovers_in_a_later_round(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    """Each image already retries MAX_IMAGE_ATTEMPTS times on its own; this
    checks the chapter-level round on top of that also gets a fair shot --
    an image that only starts succeeding after the per-image retries for
    everyone else have already run out its own budget."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/flaky.jpg"):
            attempts["n"] += 1
            if attempts["n"] <= common.MAX_IMAGE_ATTEMPTS:
                return httpx.Response(500)
        return httpx.Response(200, content=jpeg_bytes)

    async def fetch(_client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        return [f"{chapter_url}/flaky.jpg"]

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=fetch,
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(handler) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats == {
        "chapters": 1,
        "images": 1,
        "failed_chapters": 0,
        "complete_chapters": ["ch1"],
        "incomplete_chapters": [],
    }
    assert attempts["n"] == common.MAX_IMAGE_ATTEMPTS + 1


async def test_download_chapter_reports_incomplete_after_exhausting_all_rounds(
    tmp_path: Path, mock_client, monkeypatch: pytest.MonkeyPatch
):
    errors: list[str] = []
    monkeypatch.setattr(common, "cerror", errors.append)

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(2),
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(500)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats == {
        "chapters": 0,
        "images": 0,
        "failed_chapters": 0,
        "complete_chapters": [],
        "incomplete_chapters": ["ch1"],
    }
    assert any("INCOMPLETE" in e and "ch1" in e for e in errors)


# ---- chapter listing retry ---------------------------------------------------


async def test_download_chapter_retries_a_failing_listing_fetch(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    calls = {"n": 0}

    async def fetch(_client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        calls["n"] += 1
        if calls["n"] < common.LISTING_RETRY_ATTEMPTS:
            raise httpx.ConnectError("refused")
        return [f"{chapter_url}/0.jpg"]

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=fetch,
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert calls["n"] == common.LISTING_RETRY_ATTEMPTS
    assert stats["images"] == 1


async def test_download_chapter_gives_up_on_listing_after_all_attempts(
    tmp_path: Path, mock_client, monkeypatch: pytest.MonkeyPatch
):
    warnings: list[str] = []
    monkeypatch.setattr(common, "cwarning", warnings.append)
    calls = {"n": 0}

    async def fetch(_client: httpx.AsyncClient, _chapter_url: str) -> list[str]:
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=fetch,
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(200)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert calls["n"] == common.LISTING_RETRY_ATTEMPTS
    assert stats == {
        "chapters": 0,
        "images": 0,
        "failed_chapters": 0,
        "complete_chapters": [],
        "incomplete_chapters": ["ch1"],
    }
    assert any("failed to read ch1" in w for w in warnings)


async def test_download_series_warns_on_persistently_empty_chapter(
    tmp_path: Path, mock_client, monkeypatch: pytest.MonkeyPatch
):
    warnings: list[str] = []
    monkeypatch.setattr(common, "cwarning", warnings.append)

    async def _fetch(_client: httpx.AsyncClient, _url: str) -> list[str]:
        return []

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_fetch,
        chapter_folder_name=lambda url: "empty",
    )
    async with mock_client(lambda r: httpx.Response(200)) as client:
        stats = await downloader(client, ["https://fake.test/empty"], tmp_path)

    assert stats == {
        "chapters": 0,
        "images": 0,
        "failed_chapters": 0,
        "complete_chapters": [],
        "incomplete_chapters": ["empty"],
    }
    assert not (tmp_path / "empty").exists()
    assert any("no images" in w for w in warnings)


async def test_download_series_counts_unexpected_chapter_error(
    tmp_path: Path, mock_client, monkeypatch: pytest.MonkeyPatch
):
    errors: list[str] = []
    monkeypatch.setattr(common, "cerror", errors.append)

    async def _fetch(_client: httpx.AsyncClient, _url: str) -> list[str]:
        raise RuntimeError("boom")

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_fetch,
        chapter_folder_name=lambda url: "broken",
    )
    async with mock_client(lambda r: httpx.Response(200)) as client:
        stats = await downloader(client, ["https://fake.test/broken"], tmp_path)

    assert stats == {
        "chapters": 0,
        "images": 0,
        "failed_chapters": 1,
        "complete_chapters": [],
        "incomplete_chapters": [],
    }
    assert errors


# ---- incomplete_chapters.json report ----------------------------------------


async def test_incomplete_report_written_and_merged_across_runs(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    # Run 1: chapter "bad" fails completely, chapter "good" succeeds.
    async def fetch(_client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        return [f"{chapter_url}/0.jpg"]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/bad/" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, content=jpeg_bytes)

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=fetch,
        chapter_folder_name=lambda url: url.rsplit("/", 1)[-1],
    )
    async with mock_client(handler) as client:
        await downloader(client, ["https://fake.test/bad", "https://fake.test/good"], tmp_path)

    report_path = tmp_path / "incomplete_chapters.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    folders = {c["folder"] for c in report["chapters"]}
    assert folders == {"bad"}

    # Run 2 (e.g. a re-run later): "bad" now succeeds -- the report must be
    # cleared, not just left stale from run 1.
    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        await downloader(client, ["https://fake.test/bad"], tmp_path)

    assert not report_path.exists()


async def test_incomplete_report_preserves_unrelated_entries(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    """A shared out_dir (e.g. mgread's single_chapters/) can hold results
    from unrelated runs; a later run must not erase entries it didn't touch."""
    report_path = tmp_path / "incomplete_chapters.json"
    report_path.write_text(
        json.dumps({"chapters": [{"folder": "other", "chapter_url": "x", "downloaded": 0, "total": 1, "missing": 1}]}),
        encoding="utf-8",
    )

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(1),
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        await downloader(client, ["https://fake.test/ch1"], tmp_path)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert {c["folder"] for c in report["chapters"]} == {"other"}


async def test_dry_run_does_not_write_incomplete_report(tmp_path: Path, mock_client):
    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(2),
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(500)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path, dry_run=True)

    assert stats["chapters"] == 1
    assert not (tmp_path / "incomplete_chapters.json").exists()
    assert not (tmp_path / "ch1").exists()


# ---- chapter_manifest.json: skip the listing fetch for verified chapters ----


async def test_rerun_skips_listing_fetch_for_a_manifest_verified_chapter(
    tmp_path: Path, mock_client, jpeg_bytes: bytes
):
    calls = {"n": 0}

    async def fetch(_client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        calls["n"] += 1
        return [f"{chapter_url}/{i}.jpg" for i in range(2)]

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=fetch,
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        stats1 = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats1 == {
        "chapters": 1,
        "images": 2,
        "failed_chapters": 0,
        "complete_chapters": ["ch1"],
        "incomplete_chapters": [],
    }
    assert calls["n"] == 1
    manifest_path = tmp_path / "chapter_manifest.json"
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"chapters": {"ch1": 2}}

    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        stats2 = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats2 == {
        "chapters": 1,
        "images": 2,
        "failed_chapters": 0,
        "complete_chapters": ["ch1"],
        "incomplete_chapters": [],
    }
    assert calls["n"] == 1  # the listing page was not fetched a second time


async def test_manifest_falls_back_to_a_real_check_if_a_file_goes_missing(
    tmp_path: Path, mock_client, jpeg_bytes: bytes
):
    calls = {"n": 0}

    async def fetch(_client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        calls["n"] += 1
        return [f"{chapter_url}/{i}.jpg" for i in range(2)]

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=fetch,
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        await downloader(client, ["https://fake.test/ch1"], tmp_path)
    assert calls["n"] == 1

    (tmp_path / "ch1" / "0002.jpg").unlink()

    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert calls["n"] == 2  # manifest didn't match on disk, so it re-fetched the listing
    assert stats == {
        "chapters": 1,
        "images": 2,
        "failed_chapters": 0,
        "complete_chapters": ["ch1"],
        "incomplete_chapters": [],
    }
    assert (tmp_path / "ch1" / "0002.jpg").exists()


async def test_manifest_entry_is_dropped_when_a_chapter_goes_incomplete(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(2),
        chapter_folder_name=lambda url: "ch1",
    )
    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        await downloader(client, ["https://fake.test/ch1"], tmp_path)
    manifest_path = tmp_path / "chapter_manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"chapters": {"ch1": 2}}

    (tmp_path / "ch1" / "0002.jpg").unlink()
    async with mock_client(lambda r: httpx.Response(500)) as client:
        await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert not manifest_path.exists()


# ---- bookkeeping writes and folder collisions ----------------------------------


def test_a_failed_report_write_leaves_the_previous_report_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An unreadable incomplete_chapters.json reads as "nothing incomplete" (here and in OmniScan's importer),
    so a write cut short by a crash or a full disk must never replace the old report with half a file."""
    previous = common.ChapterResult("ch1", "https://fake.test/ch1", ok=1, total=3, complete=False)
    common._write_incomplete_report(tmp_path, [previous])
    report = tmp_path / "incomplete_chapters.json"
    before = report.read_text(encoding="utf-8")
    real_write_text = Path.write_text

    def disk_full(self: Path, data: str, *args, **kwargs) -> int:
        real_write_text(self, data[:10], *args, **kwargs)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", disk_full)
    later = common.ChapterResult("ch2", "https://fake.test/ch2", ok=0, total=2, complete=False)
    with pytest.raises(OSError):
        common._write_incomplete_report(tmp_path, [later])

    assert report.read_text(encoding="utf-8") == before
    assert json.loads(before)["chapters"][0]["folder"] == "ch1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["incomplete_chapters.json"]  # no half-written leftovers


async def test_two_urls_for_one_folder_download_once(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    """The same chapter listed under two URLs must not be fetched into one folder twice at once."""
    fetched: list[str] = []

    async def fetch(_client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        fetched.append(chapter_url)
        return [f"{chapter_url}/0.jpg"]

    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=fetch,
        chapter_folder_name=lambda url: "num2_chapter",
    )
    urls = ["https://fake.test/view?num=2", "https://fake.test/view?num=2&ref=list"]
    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        stats = await downloader(client, urls, tmp_path)

    assert fetched == ["https://fake.test/view?num=2"]
    assert stats["complete_chapters"] == ["num2_chapter"]


# ---- pages are named after what they are, not after their URL -------------------------------


def _image_bytes(fmt: str) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.effect_noise((48, 48), 40).convert("RGB").save(buf, format=fmt)
    return buf.getvalue()


async def _download_one(tmp_path: Path, mock_client, url: str, handler) -> dict:
    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=lambda _client, _chapter: _async([url]),
        chapter_folder_name=lambda _url: "ch1",
    )
    async with mock_client(handler) as client:
        return await downloader(client, ["https://fake.test/ch1"], tmp_path)


async def _async(value):
    return value


@pytest.mark.parametrize(
    ("url", "fmt", "saved"),
    [
        ("https://cdn.test/image.php?id=3", "PNG", "0001.png"),  # a script URL: the suffix is no image type
        ("https://cdn.test/pages/1.jpg", "WEBP", "0001.webp"),  # a CDN answering a .jpg URL with WebP
        ("https://cdn.test/pages/1", "JPEG", "0001.jpg"),  # no suffix at all
        ("https://cdn.test/pages/1.jpeg", "JPEG", "0001.jpeg"),  # a suffix that already names the format stays
        ("https://cdn.test/pages/1.bmp", "BMP", "0001.bmp"),
        ("https://cdn.test/pages/1.jfif", "JPEG", "0001.jpg"),  # OmniScan's importer does not read .jfif
    ],
)
async def test_a_page_is_saved_under_the_extension_of_its_real_format(
    tmp_path: Path, mock_client, url: str, fmt: str, saved: str
):
    body = _image_bytes(fmt)
    stats = await _download_one(tmp_path, mock_client, url, lambda r: httpx.Response(200, content=body))

    assert stats["complete_chapters"] == ["ch1"]
    assert sorted(p.name for p in (tmp_path / "ch1").iterdir()) == [saved]


async def test_resume_finds_a_page_saved_under_another_extension(tmp_path: Path, mock_client):
    (tmp_path / "ch1").mkdir()
    (tmp_path / "ch1" / "0001.png").write_bytes(_image_bytes("PNG"))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404)

    stats = await _download_one(tmp_path, mock_client, "https://cdn.test/image.php?id=3", handler)

    assert requests == [] and stats["complete_chapters"] == ["ch1"]


async def test_a_broken_leftover_under_another_extension_is_replaced(tmp_path: Path, mock_client):
    (tmp_path / "ch1").mkdir()
    (tmp_path / "ch1" / "0001.jpg").write_bytes(FAKE_JPEG_BYTES)  # truncated garbage from an earlier try
    body = _image_bytes("PNG")

    await _download_one(
        tmp_path, mock_client, "https://cdn.test/pages/1.jpg", lambda r: httpx.Response(200, content=body)
    )

    assert sorted(p.name for p in (tmp_path / "ch1").iterdir()) == ["0001.png"]
