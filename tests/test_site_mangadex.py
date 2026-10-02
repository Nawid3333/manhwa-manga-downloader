"""Tests for the MangaDex driver: URL shapes, feed paging/dedupe/language
selection, the MangaDex@Home image resolution, 429 handling and the
bare-chapter-URL flow -- all via httpx.MockTransport."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.sites.mangadex import API, SITE, MangaDexDriver, mangadex_langs, user_agent

MANGA_ID = "8f3e1818-a015-491d-bd81-3addc4d7d56a"
CH1 = "11111111-1111-4111-8111-111111111111"
CH2 = "22222222-2222-4222-8222-222222222222"
CH2_DUP = "33333333-3333-4333-8333-333333333333"
CH_ONESHOT = "44444444-4444-4444-8444-444444444444"

LIST_URL = f"{SITE}/title/{MANGA_ID}/hyouka"
CHAPTER_URL = f"{SITE}/chapter/{CH1}"


def _feed_record(chapter_id: str, chapter: str | None, *, external: str | None = None, pages: int = 20) -> dict:
    attributes = {"chapter": chapter, "translatedLanguage": "en", "externalUrl": external, "pages": pages}
    return {"id": chapter_id, "type": "chapter", "attributes": attributes}


def test_classify_and_slug():
    driver = MangaDexDriver()
    assert driver.classify(LIST_URL) == "list"
    assert driver.classify(f"{SITE}/title/{MANGA_ID}") == "list"
    assert driver.classify(CHAPTER_URL) == "chapter"
    assert driver.classify(f"{CHAPTER_URL}/3") == "chapter"
    assert driver.classify(f"{SITE}/titles/latest") == "unknown"
    assert driver.series_slug(LIST_URL) == MANGA_ID


def test_user_agent_is_descriptive():
    ua = user_agent()
    assert ua.startswith("manhwa-manga-downloader/")
    assert "github.com/Nawid3333/manhwa-manga-downloader" in ua
    assert MangaDexDriver.extra_headers == {"User-Agent": ua}


def test_langs_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MANGADEX_LANGS", raising=False)
    assert mangadex_langs() == ["en"]
    monkeypatch.setenv("MANGADEX_LANGS", "ja, pt-br,,")
    assert mangadex_langs() == ["ja", "pt-br"]


def test_folder_name_before_and_after_the_number_is_known():
    driver = MangaDexDriver()
    assert driver.folder_name(CHAPTER_URL) == "num0_Chapter 11111111"
    driver._numbers[CH1] = 12.5
    assert driver.folder_name(CHAPTER_URL) == "num12.5_Chapter 12.5"
    driver._numbers[CH1] = 7.0
    assert driver.folder_name(f"{CHAPTER_URL}/2") == "num7_Chapter 7"


async def test_list_chapters_pages_by_total_and_dedupes(mock_client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MANGADEX_LANGS", "ja,en")
    driver = MangaDexDriver()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.url.params["offset"])
        if offset == 0:
            data = [_feed_record(CH2, "2"), _feed_record(CH_ONESHOT, None), _feed_record(CH2_DUP, "2")]
        else:
            data = [_feed_record(CH1, "1")]
        return httpx.Response(200, json={"data": data, "limit": 500, "offset": offset, "total": 4})

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, LIST_URL)

    assert chapters == [(f"{SITE}/chapter/{CH1}", 1.0), (f"{SITE}/chapter/{CH2}", 2.0)]
    assert [int(r.url.params["offset"]) for r in requests] == [0, 3]
    first = requests[0]
    assert str(first.url).startswith(f"{API}/manga/{MANGA_ID}/feed?")
    assert first.url.params.get_list("translatedLanguage[]") == ["ja", "en"]
    assert first.url.params.get_list("contentRating[]") == ["safe", "suggestive", "erotica", "pornographic"]
    assert first.url.params["order[chapter]"] == "asc"
    assert driver._numbers[CH1] == 1.0


async def test_image_urls_from_at_home_server(mock_client):
    driver = MangaDexDriver()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{API}/at-home/server/{CH1}"
        return httpx.Response(
            200,
            json={"baseUrl": "https://node.mangadex.network", "chapter": {"hash": "abc", "data": ["1.png", "2.jpg"]}},
        )

    async with mock_client(handler) as client:
        urls = await driver.image_urls(client, CHAPTER_URL)

    assert urls == ["https://node.mangadex.network/data/abc/1.png", "https://node.mangadex.network/data/abc/2.jpg"]


async def test_api_get_retries_after_429(mock_client):
    driver = MangaDexDriver()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    async with mock_client(handler) as client:
        assert await driver.api_get(client, "/ping") == {"ok": True}
    assert attempts == 2


async def test_api_get_raises_on_hard_error(mock_client):
    driver = MangaDexDriver()
    async with mock_client(lambda r: httpx.Response(404, json={})) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await driver.api_get(client, "/missing")


async def test_bare_chapter_url_learns_its_number_then_downloads(tmp_path: Path, jpeg_bytes: bytes):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["User-Agent"] == user_agent()
        url = str(request.url)
        if url == f"{API}/chapter/{CH1}":
            return httpx.Response(200, json={"data": _feed_record(CH1, "5")})
        if url == f"{API}/at-home/server/{CH1}":
            return httpx.Response(
                200, json={"baseUrl": "https://node.test", "chapter": {"hash": "h", "data": ["a.jpg"]}}
            )
        if url == "https://node.test/data/h/a.jpg":
            return httpx.Response(200, content=jpeg_bytes)
        return httpx.Response(404)

    class MockedDriver(MangaDexDriver):
        def client(self, **kwargs) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=self.extra_headers)

    driver = MockedDriver()
    stats = await driver.download_series_url(CHAPTER_URL, tmp_path)

    assert stats == {"chapters": 1, "images": 1, "failed_chapters": 0, "incomplete_chapters": []}
    assert (tmp_path / "num5_Chapter 5" / "0001.jpg").exists()
    assert seen[0] == f"{API}/chapter/{CH1}"
    assert json.loads((tmp_path / "chapter_manifest.json").read_text())["chapters"] == {"num5_Chapter 5": 1}


async def test_list_chapters_skips_external_and_empty_uploads(mock_client):
    """An official chapter linking out to the publisher must not win the dedupe over a hosted upload."""
    driver = MangaDexDriver()
    ch3_external = "55555555-5555-4555-8555-555555555555"
    ch3_hosted = "66666666-6666-4666-8666-666666666666"
    ch4_empty = "77777777-7777-4777-8777-777777777777"

    def handler(request: httpx.Request) -> httpx.Response:
        data = [
            _feed_record(CH1, "1"),
            _feed_record(ch3_external, "3", external="https://mangaplus.shueisha.co.jp/viewer/1", pages=0),
            _feed_record(ch3_hosted, "3"),
            _feed_record(ch4_empty, "4", pages=0),
        ]
        return httpx.Response(200, json={"data": data, "limit": 500, "offset": 0, "total": 4})

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, LIST_URL)

    assert chapters == [(f"{SITE}/chapter/{CH1}", 1.0), (f"{SITE}/chapter/{ch3_hosted}", 3.0)]
