"""Tests for the wfwf (늑대닷컴) driver: numbered domains, URL classification, list/pagination/image
parsing (query-string chapter numbers, #vimg-area image containers) -- via
httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest

from src.sites.wfwf import BASE, WfwfDriver

driver = WfwfDriver()

LIST_URL = f"{BASE}/list?toon=123&s=o&pg=1"
CHAPTER_URL = f"{BASE}/view?toon=123&num=45"


def test_classify_list_and_chapter_urls():
    assert driver.classify(LIST_URL) == "list"
    assert driver.classify(CHAPTER_URL) == "chapter"
    assert driver.classify(f"{BASE}/") == "unknown"


def test_series_slug_reads_toon_param():
    assert driver.series_slug(LIST_URL) == "123"


def test_series_slug_missing_toon_raises():
    with pytest.raises(ValueError):
        driver.series_slug(f"{BASE}/list?s=o")


def test_folder_name_reads_num_param():
    assert driver.folder_name(CHAPTER_URL) == "num45_chapter"


def test_folder_name_defaults_to_unknown():
    assert driver.folder_name(f"{BASE}/view?toon=123") == "numunknown_chapter"


def test_normalize_relative_and_absolute_hrefs():
    assert driver._normalize("/view?toon=1") == f"{BASE}/view?toon=1"
    assert driver._normalize("view?toon=1") == f"{BASE}/view?toon=1"
    assert driver._normalize("https://elsewhere.example/x") == "https://elsewhere.example/x"


def test_num_from_title_extracts_leading_number():
    assert driver._num_from_title("Chapter 12.5 - finale") == 12.5
    assert driver._num_from_title("no digits here") == 0.0


def test_parse_chapter_links_filters_to_ep_item():
    html = """
    <div class="list-sec">
      <a class="ep-item" href="/view?toon=123&num=2">Chapter 2</a>
      <a class="other" href="/view?toon=123&num=1">Not an episode</a>
    </div>
    """
    links = driver._parse_chapter_links(html)
    assert links == [("/view?toon=123&num=2", "Chapter 2")]


def test_parse_pagination_pages_reads_pg_buttons():
    html = """
    <div class="pagi-wrap">
      <a class="pg-btn" href="/list?toon=123&pg=2">2</a>
      <a class="pg-btn" href="/list?toon=123&pg=3">3</a>
      <a class="other" href="/list?toon=123&pg=9">skip</a>
    </div>
    """
    assert driver._parse_pagination_pages(html) == [1, 2, 3]


def test_parse_image_urls_prefers_data_src_then_data_original_then_src():
    html = """
    <div id="vimg-area">
      <img data-src="https://cdn/1.jpg" data-original="https://cdn/1-orig.jpg" src="https://cdn/1-fallback.jpg" />
      <img data-original="https://cdn/2.jpg" src="https://cdn/2-fallback.jpg" />
      <img src="https://cdn/3.jpg" />
    </div>
    """
    assert driver._parse_image_urls(html) == ["https://cdn/1.jpg", "https://cdn/2.jpg", "https://cdn/3.jpg"]


def test_parse_image_urls_missing_area_returns_empty():
    assert driver._parse_image_urls("<html></html>") == []


async def test_image_urls_fetches_and_parses(mock_client):
    html = '<div id="vimg-area"><img data-src="https://cdn/0.jpg" /></div>'
    async with mock_client(lambda r: httpx.Response(200, text=html)) as client:
        urls = await driver.image_urls(client, CHAPTER_URL)
    assert urls == ["https://cdn/0.jpg"]


async def test_list_chapters_merges_pages_in_site_order(mock_client):
    """list_chapters doesn't re-sort by chapter number -- it trusts
    ascending page order plus the site's own oldest-first sort (`s=o`)."""
    page1 = """
    <div class="list-sec"><a class="ep-item" href="/view?toon=123&num=1">Chapter 1</a></div>
    <div class="pagi-wrap"><a class="pg-btn" href="/list?toon=123&pg=2">2</a></div>
    """
    page2 = '<div class="list-sec"><a class="ep-item" href="/view?toon=123&num=2">Chapter 2</a></div>'

    def handler(request: httpx.Request) -> httpx.Response:
        if "pg=2" in str(request.url):
            return httpx.Response(200, text=page2)
        return httpx.Response(200, text=page1)

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, LIST_URL)

    assert [num for _, num in chapters] == [1.0, 2.0]


async def test_list_chapters_fetches_each_list_page_once(mock_client):
    """Page 1 is fetched for its pagination and must not be fetched again for its chapters."""
    page1 = """
    <div class="list-sec"><a class="ep-item" href="/view?toon=123&num=1">Chapter 1</a></div>
    <div class="pagi-wrap">
      <a class="pg-btn" href="/list?toon=123&pg=1">1</a>
      <a class="pg-btn" href="/list?toon=123&pg=2">2</a>
    </div>
    """
    page2 = '<div class="list-sec"><a class="ep-item" href="/view?toon=123&num=2">Chapter 2</a></div>'
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["pg"]
        fetched.append(page)
        return httpx.Response(200, text=page2 if page == "2" else page1)

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, LIST_URL)

    assert [num for _, num in chapters] == [1.0, 2.0]
    assert sorted(fetched) == ["1", "2"]


# ---- numbered addresses: the site moves from wfwf<N>.com to wfwf<N+k>.com ----


@pytest.mark.parametrize("host", ["wfwf504.com", "wfwf510.com", "www.wfwf999.com", "WFWF510.COM"])
def test_matches_every_numbered_address(host: str):
    assert driver.matches(f"https://{host}/list?toon=1")


@pytest.mark.parametrize("host", ["wfwf.com", "wfwfx.com", "evilwfwf510.com", "wfwf510.com.example"])
def test_does_not_match_lookalikes(host: str):
    assert not driver.matches(f"https://{host}/list?toon=1")


async def test_list_chapters_stays_on_the_address_it_was_given(mock_client):
    page = '<div class="list-sec"><a class="ep-item" href="/view?toon=7&num=1">1</a></div>'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host)
        return httpx.Response(200, text=page)

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, "https://wfwf777.com/list?toon=7")

    assert requested == ["wfwf777.com"]
    assert chapters == [("https://wfwf777.com/view?toon=7&num=1", 1.0)]
    assert driver.referer_for("https://wfwf777.com/view?toon=7&num=1") == "https://wfwf777.com/"


MOVED_NOTICE = """
<div class="card"><div class="title">늑대닷컴 접속 주소 안내</div>
<a href="https://wfwf510.com" class="main-btn">새로운 주소로 이동</a></div>
"""


@pytest.mark.parametrize("url", ["https://wfwf504.com/list?toon=7", "https://wfwf504.com/view?toon=7&num=1"])
async def test_an_old_address_names_the_new_one(mock_client, url: str):
    async with mock_client(lambda r: httpx.Response(200, text=MOVED_NOTICE)) as client:
        with pytest.raises(RuntimeError, match=r"wfwf504\.com has moved to https://wfwf510\.com"):
            if driver.classify(url) == "list":
                await driver.list_chapters(client, url)
            else:
                await driver.image_urls(client, url)
