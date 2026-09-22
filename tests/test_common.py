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


def _make_fetch(count: int):
    async def _fetch(_client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        return [f"{chapter_url}/{i}.jpg" for i in range(count)]

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


# ---- make_downloader / download_series: happy path --------------------------


async def test_download_series_writes_images(tmp_path: Path, mock_client, jpeg_bytes: bytes):
    downloader = common.make_downloader(
        site_label="fake",
        fetch_image_urls=_make_fetch(2),
        chapter_folder_name=lambda url: url.rsplit("/", 1)[-1],
    )

    async with mock_client(lambda r: httpx.Response(200, content=jpeg_bytes)) as client:
        stats = await downloader(client, ["https://fake.test/ch1"], tmp_path)

    assert stats == {"chapters": 1, "images": 2, "failed_chapters": 0, "incomplete_chapters": []}
    assert (tmp_path / "ch1" / "0001.jpg").exists()
    assert (tmp_path / "ch1" / "0002.jpg").exists()
    assert not (tmp_path / "ch1" / "incomplete_chapters.json").exists()
    assert not (tmp_path / "incomplete_chapters.json").exists()


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
    assert stats == {"chapters": 1, "images": 1, "failed_chapters": 0, "incomplete_chapters": []}
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

    assert stats == {"chapters": 1, "images": 1, "failed_chapters": 0, "incomplete_chapters": []}
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

    assert stats == {"chapters": 0, "images": 0, "failed_chapters": 0, "incomplete_chapters": ["ch1"]}
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
    assert stats == {"chapters": 0, "images": 0, "failed_chapters": 0, "incomplete_chapters": ["ch1"]}
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

    assert stats == {"chapters": 0, "images": 0, "failed_chapters": 0, "incomplete_chapters": ["empty"]}
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

    assert stats == {"chapters": 0, "images": 0, "failed_chapters": 1, "incomplete_chapters": []}
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
