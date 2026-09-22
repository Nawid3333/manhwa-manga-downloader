"""Tests for the wfwf504.com driver: URL classification, list/pagination/image
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
