"""Tests for the mgread.io driver: URL classification, chapter link/pagination
parsing, and reader-page image extraction -- via httpx.MockTransport."""

from __future__ import annotations

import httpx

from src.sites.mgread import BASE, IMG_HOST, MgreadDriver

driver = MgreadDriver()

LIST_URL = f"{BASE}/manga/one-piece"
CHAPTER_URL = f"{BASE}/manga/one-piece/chapter-5.5"


def test_classify_chapter_and_list_urls():
    assert driver.classify(CHAPTER_URL) == "chapter"
    assert driver.classify(LIST_URL) == "list"
    assert driver.classify(f"{BASE}/search?q=x") == "unknown"


def test_series_slug():
    assert driver.series_slug(LIST_URL) == "one-piece"


def test_chapter_num_from_url():
    assert driver.chapter_num_from_url(CHAPTER_URL) == 5.5
    assert driver.chapter_num_from_url(LIST_URL) is None


def test_chapter_label_strips_trailing_zeros():
    assert driver.chapter_label(5.0) == "5"
    assert driver.chapter_label(5.5) == "5.5"
    assert driver.chapter_label(5.25) == "5.25"


def test_folder_name():
    assert driver.folder_name(CHAPTER_URL) == "num5.5_Chapter 5.5"
    assert driver.folder_name(LIST_URL) == "num0_Chapter unknown"


def test_parse_chapter_links_extracts_and_dedupes():
    html = """
    <div id="chapter-list">
      <a href="/manga/one-piece/chapter-2">Chapter 2</a>
      <a href="/manga/one-piece/chapter-1">Chapter 1</a>
      <a href="/manga/one-piece/chapter-1">Chapter 1 (dup)</a>
      <a href="/manga/one-piece/not-a-chapter">Extra</a>
    </div>
    """
    links = driver.parse_chapter_links(html)
    assert [num for _, num in links] == [2.0, 1.0]
    assert links[0][0] == f"{BASE}/manga/one-piece/chapter-2"


def test_parse_pagination_pages_fills_ellipsis_gap():
    html = """
    <nav aria-label="Pagination">
      <a href="/manga/one-piece/chapter/page/3/">3</a>
      <a href="/manga/one-piece/chapter/page/9/">9</a>
    </nav>
    """
    assert driver.parse_pagination_pages(html) == list(range(1, 10))


def test_parse_pagination_pages_defaults_to_first_page():
    assert driver.parse_pagination_pages("<html></html>") == [1]


async def test_image_urls_prefers_data_original_src(mock_client):
    html = f"""
    <html><body>
      <img data-original-src="{IMG_HOST}/1/5/0.jpg" src="placeholder.jpg" />
      <img src="{IMG_HOST}/1/5/1.jpg" />
      <img src="data:image/gif;base64,xxxx" />
      <img src="https://unrelated.example/2.jpg" />
    </body></html>
    """

    async with mock_client(lambda r: httpx.Response(200, text=html)) as client:
        urls = await driver.image_urls(client, CHAPTER_URL)

    assert urls == [f"{IMG_HOST}/1/5/0.jpg", f"{IMG_HOST}/1/5/1.jpg"]


async def test_list_chapters_merges_pagination_and_sorts(mock_client):
    first_page = """
    <html><body>
      <div id="chapter-list"><a href="/manga/one-piece/chapter-1">Chapter 1</a></div>
      <nav aria-label="Pagination"><a href="/manga/one-piece/chapter/page/2/">2</a></nav>
    </body></html>
    """
    second_page = '<div id="chapter-list"><a href="/manga/one-piece/chapter-2">Chapter 2</a></div>'

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/page/2/"):
            return httpx.Response(200, text=second_page)
        return httpx.Response(200, text=first_page)

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, LIST_URL)

    assert [num for _, num in chapters] == [1.0, 2.0]
