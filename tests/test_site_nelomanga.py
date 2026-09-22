"""Tests for the nelomanga.net driver: URL classification, folder naming,
CDN page-count probing (including the 429-vs-"unreachable" regression this
run's fix addresses), and chapter listing -- all against
httpx.MockTransport, never the live site."""

from __future__ import annotations

import httpx
import pytest

from src.sites.nelomanga import CDN_HOSTS, NelomangaDriver

driver = NelomangaDriver()

CHAPTER_URL = "https://www.nelomanga.net/manga/death-note/chapter-37"
FRACTIONAL_CHAPTER_URL = "https://www.nelomanga.net/manga/death-note/chapter-194-1"
LIST_URL = "https://www.nelomanga.net/manga/death-note"


# ---- URL classification -----------------------------------------------------


def test_classify_chapter_url():
    assert driver.classify(CHAPTER_URL) == "chapter"


def test_classify_list_url():
    assert driver.classify(LIST_URL) == "list"


def test_classify_unknown_url():
    assert driver.classify("https://www.nelomanga.net/search?q=x") == "unknown"


def test_chapter_label_converts_dashes_to_dots():
    assert driver.chapter_label(FRACTIONAL_CHAPTER_URL) == "194.1"
    assert driver.chapter_label(CHAPTER_URL) == "37"


def test_chapter_label_none_for_non_chapter_url():
    assert driver.chapter_label(LIST_URL) is None


def test_chapter_num():
    assert driver.chapter_num(FRACTIONAL_CHAPTER_URL) == 194.1


def test_series_slug_from_list_and_chapter_urls():
    assert driver.series_slug(LIST_URL) == "death-note"
    assert driver.series_slug(CHAPTER_URL) == "death-note"


def test_folder_name_for_chapter():
    assert driver.folder_name(CHAPTER_URL) == "num37_Chapter 37"


def test_folder_name_fallback_when_unrecognized():
    assert driver.folder_name(LIST_URL) == "num0_death-note"


# ---- _head_ok: the 429-aware retry loop --------------------------------------


async def test_head_ok_true_on_200(mock_client):
    async with mock_client(lambda r: httpx.Response(200)) as client:
        assert await driver._head_ok(client, "https://x/0.webp") is True


async def test_head_ok_false_on_404_without_retry(mock_client):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    async with mock_client(handler) as client:
        assert await driver._head_ok(client, "https://x/0.webp") is False
    assert calls["n"] == 1


async def test_head_ok_retries_past_429_then_succeeds(mock_client):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200)

    async with mock_client(handler) as client:
        assert await driver._head_ok(client, "https://x/0.webp") is True
    assert calls["n"] == 3


async def test_head_ok_gives_up_after_persistent_429(mock_client):
    async with mock_client(lambda r: httpx.Response(429)) as client:
        assert await driver._head_ok(client, "https://x/0.webp") is False


async def test_head_ok_false_on_transport_error(mock_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with mock_client(handler) as client:
        assert await driver._head_ok(client, "https://x/0.webp") is False


# ---- image_urls: CDN probing -------------------------------------------------


def _cdn_handler(host: str, page_count: int, flaky_indices: frozenset[int] = frozenset()):
    """Serve `page_count` pages from `host`; 404 everywhere else. Indices in
    `flaky_indices` return one 429 before finally answering -- the exact
    rate-limiting shape that used to be misread as "chapter doesn't exist"."""
    served_flaky: set[int] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not url.startswith(host):
            return httpx.Response(404)
        idx = int(url.rsplit("/", 1)[-1].removesuffix(".webp"))
        if idx in flaky_indices and idx not in served_flaky:
            served_flaky.add(idx)
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200 if idx < page_count else 404)

    return handler


async def test_image_urls_finds_page_count_via_binary_search(mock_client):
    async with mock_client(_cdn_handler(CDN_HOSTS[0], page_count=17)) as client:
        urls = await driver.image_urls(client, CHAPTER_URL)
    assert len(urls) == 17
    assert urls[0] == f"{CDN_HOSTS[0]}/death-note/37/0.webp"
    assert urls[-1] == f"{CDN_HOSTS[0]}/death-note/37/16.webp"


async def test_image_urls_survives_rate_limiting_during_probe(mock_client):
    """Regression test for the death-note run: a 429 on the HEAD probe used
    to make the whole chapter look CDN-unreachable instead of retrying."""
    handler = _cdn_handler(CDN_HOSTS[0], page_count=17, flaky_indices=frozenset({0, 1, 8, 12}))
    async with mock_client(handler) as client:
        urls = await driver.image_urls(client, CHAPTER_URL)
    assert len(urls) == 17


async def test_image_urls_falls_back_to_second_cdn_host(mock_client):
    async with mock_client(_cdn_handler(CDN_HOSTS[1], page_count=5)) as client:
        urls = await driver.image_urls(client, CHAPTER_URL)
    assert len(urls) == 5
    assert urls[0].startswith(CDN_HOSTS[1])


async def test_image_urls_warns_when_no_cdn_host_has_the_chapter(mock_client, monkeypatch: pytest.MonkeyPatch):
    warnings: list[str] = []
    monkeypatch.setattr("src.sites.nelomanga.cwarning", warnings.append)

    async with mock_client(lambda r: httpx.Response(404)) as client:
        urls = await driver.image_urls(client, CHAPTER_URL)

    assert urls == []
    assert any("CDN unreachable" in w for w in warnings)


# ---- list_chapters ------------------------------------------------------------


def _chapters_page(chapters: list[dict], has_more: bool) -> httpx.Response:
    return httpx.Response(200, json={"data": {"chapters": chapters, "pagination": {"has_more": has_more}}})


async def test_list_chapters_paginates_sorts_and_dedupes(mock_client):
    page1 = [{"chapter_slug": "chapter-2"}, {"chapter_slug": "chapter-1"}]
    page2 = [{"chapter_slug": "chapter-3"}, {"chapter_slug": "chapter-1"}]  # duplicate

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        return _chapters_page(page1, has_more=True) if offset == 0 else _chapters_page(page2, has_more=False)

    async with mock_client(handler) as client:
        result = await driver.list_chapters(client, LIST_URL)

    assert [num for _, num in result] == [1.0, 2.0, 3.0]
    assert result[0][0].endswith("/chapter-1")
