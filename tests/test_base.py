"""Tests for src/base.py: SiteDriver.matches(), the select_chapters filter,
and the download_series_url facade wired to an in-test fake driver (via
httpx.MockTransport, no live network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from src.base import SiteDriver


class FakeDriver(SiteDriver):
    key = "fake"
    name = "Fake Site"
    domains = ("fake.test",)
    referer = "https://fake.test/"

    def __init__(self, handler):
        self._handler = handler

    def client(self, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))

    def classify(self, url: str) -> str:
        return "chapter" if "/chapter/" in url else "list"

    def series_slug(self, url: str) -> str:
        return "series"

    async def list_chapters(self, client, url):
        return [("https://fake.test/chapter/1", 1.0), ("https://fake.test/chapter/2", 2.0)]

    async def image_urls(self, client, chapter_url):
        return [f"{chapter_url}/0.jpg"]

    def folder_name(self, chapter_url: str) -> str:
        return chapter_url.rsplit("/", 1)[-1]


def _ok_handler(jpeg_bytes: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=jpeg_bytes)

    return handler


# ---- matches() ------------------------------------------------------------


def test_matches_exact_and_subdomain():
    driver = FakeDriver(lambda r: httpx.Response(200))
    assert driver.matches("https://fake.test/x") is True
    assert driver.matches("https://www.fake.test/x") is True
    assert driver.matches("https://notfake.test/x") is False


# ---- series_folder() fallback and path-traversal guard ---------------------


def test_series_folder_falls_back_when_slug_unavailable():
    class NoSlugDriver(SiteDriver):
        key = "noslug"
        domains = ("noslug.test",)

    assert NoSlugDriver().series_folder("https://noslug.test/x") == "series"


def test_series_folder_sanitizes_a_malicious_slug():
    """A driver whose series_slug echoes untrusted URL data (e.g. a decoded
    query parameter) must not be able to make series_folder() escape
    downloads/ -- series_folder() is the one place every driver's slug is
    turned into a real path component."""

    class MaliciousSlugDriver(SiteDriver):
        key = "evil"
        domains = ("evil.test",)

        def series_slug(self, url: str) -> str:
            return "../../../../escape"

    assert MaliciousSlugDriver().series_folder("https://evil.test/x") == "escape"


def test_single_chapter_folder_default():
    assert FakeDriver(lambda r: httpx.Response(200)).single_chapter_folder() == "single_chapters"


# ---- select_chapters() ------------------------------------------------------


def test_select_chapters_returns_all_when_none_requested():
    driver = FakeDriver(lambda r: httpx.Response(200))
    chapters = [("u1", 1.0), ("u2", 2.0)]
    assert driver.select_chapters(chapters, None) == ["u1", "u2"]


def test_select_chapters_filters_by_integer_chapter_number():
    driver = FakeDriver(lambda r: httpx.Response(200))
    chapters = [("u1", 1.0), ("u1a", 1.5), ("u2", 2.0)]
    # int(1.5) == 1, so a fractional chapter rides along with its integer.
    assert driver.select_chapters(chapters, [1]) == ["u1", "u1a"]


# ---- download_series_url facade ---------------------------------------------


async def test_download_series_url_downloads_single_chapter_directly(tmp_path: Path, jpeg_bytes: bytes):
    driver = FakeDriver(_ok_handler(jpeg_bytes))
    stats = await driver.download_series_url("https://fake.test/chapter/9", tmp_path)
    assert stats == {"chapters": 1, "images": 1, "failed_chapters": 0, "incomplete_chapters": []}
    assert (tmp_path / "9" / "0001.jpg").exists()


async def test_download_series_url_filters_list_by_requested_chapters(tmp_path: Path, jpeg_bytes: bytes):
    driver = FakeDriver(_ok_handler(jpeg_bytes))
    stats = await driver.download_series_url("https://fake.test/list", tmp_path, chapters=[2])
    assert stats == {"chapters": 1, "images": 1, "failed_chapters": 0, "incomplete_chapters": []}
    assert (tmp_path / "2" / "0001.jpg").exists()
    assert not (tmp_path / "1").exists()


async def test_download_series_url_raises_when_no_chapters_found(tmp_path: Path):
    class EmptyListDriver(FakeDriver):
        async def list_chapters(self, client, url):
            return []

    driver = EmptyListDriver(lambda r: httpx.Response(200))
    with pytest.raises(RuntimeError, match="No chapters found"):
        await driver.download_series_url("https://fake.test/list", tmp_path)


async def test_download_series_url_raises_when_requested_range_matches_nothing(tmp_path: Path):
    driver = FakeDriver(lambda r: httpx.Response(200))
    with pytest.raises(RuntimeError, match="No chapters matched"):
        await driver.download_series_url("https://fake.test/list", tmp_path, chapters=[999])
