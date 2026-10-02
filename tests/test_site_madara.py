"""Tests for the Madara (WordPress theme) driver: content sniffing, URL
shapes across prefixes, the three-step chapter-listing cascade and reader
image extraction -- all via httpx.MockTransport."""

from __future__ import annotations

import httpx

from src.sites.madara import MadaraDriver

driver = MadaraDriver()

HOST = "https://madara.example"
LIST_URL = f"{HOST}/manga/solo-leveling/"
CHAPTER_URL = f"{HOST}/manga/solo-leveling/chapter-12/"


def test_identity_has_no_domains_and_outranks_generic():
    assert driver.domains == ()
    assert driver.priority > 0


def test_sniff_recognizes_theme_markers():
    assert driver.sniff(LIST_URL, '<li class="wp-manga-chapter"><a href="x">1</a></li>')
    assert driver.sniff(LIST_URL, '<script src="/wp-content/plugins/madara-core/assets/js/x.js">')
    assert driver.sniff(LIST_URL, '<body class="post-template-default single manga-page">')
    assert driver.sniff(LIST_URL, 'var manga = {"manga_id":"123"}')
    assert not driver.sniff(LIST_URL, "<html><body><p>Madara Uchiha appears in Naruto.</p></body></html>")
    assert not driver.sniff(LIST_URL, "")


def test_classify_accepts_any_single_segment_prefix():
    assert driver.classify(LIST_URL) == "list"
    assert driver.classify(f"{HOST}/comic/solo-leveling") == "list"
    assert driver.classify(f"{HOST}/webtoon/solo-leveling/chapter-3/") == "chapter"
    assert driver.classify(f"{HOST}/read/solo-leveling/ch-3-5/") == "chapter"
    assert driver.classify(f"{HOST}/manhwa/solo-leveling/chap-7/2/") == "chapter"
    assert driver.classify(f"{HOST}/") == "unknown"
    assert driver.classify(f"{HOST}/manga/solo-leveling/extra/thing/") == "unknown"


def test_parse_chapter_url_numbers():
    assert driver.parse_chapter_url(CHAPTER_URL) == ("manga", "solo-leveling", 12.0)
    assert driver.parse_chapter_url(f"{HOST}/manga/x/chapter-12-5/") == ("manga", "x", 12.5)
    assert driver.parse_chapter_url(f"{HOST}/manga/x/chapter-12-the-return/") == ("manga", "x", 12.0)
    assert driver.parse_chapter_url(f"{HOST}/manga/x/chapter-12/4/") == ("manga", "x", 12.0)
    assert driver.parse_chapter_url(LIST_URL) is None


def test_series_slug_and_url():
    assert driver.series_slug(LIST_URL) == "solo-leveling"
    assert driver.series_slug(CHAPTER_URL) == "solo-leveling"
    assert driver.series_url(f"{HOST}/comic/solo-leveling") == f"{HOST}/comic/solo-leveling/"


def test_folder_name():
    assert driver.folder_name(CHAPTER_URL) == "num12_Chapter 12"
    assert driver.folder_name(f"{HOST}/manga/x/chapter-12-5/") == "num12.5_Chapter 12.5"
    assert driver.folder_name(LIST_URL) == "num0_Chapter unknown"


def test_referer_is_the_chapter_origin():
    assert driver.referer_for(CHAPTER_URL) == f"{HOST}/"


def test_parse_chapter_links_resolves_relative_and_keeps_page_order():
    html = """
    <ul class="main version-chap">
      <li class="wp-manga-chapter"><a href="/manga/solo-leveling/chapter-2/">Chapter 2</a></li>
      <li class="wp-manga-chapter free-chap"><a href="chapter-1/">Chapter 1</a></li>
      <li class="other"><a href="/manga/solo-leveling/chapter-99/">not a chapter entry</a></li>
    </ul>
    """
    links = driver.parse_chapter_links(html, LIST_URL)
    assert links == [(f"{HOST}/manga/solo-leveling/chapter-2/", 2.0), (f"{HOST}/manga/solo-leveling/chapter-1/", 1.0)]


def test_parse_manga_id_sources():
    assert driver.parse_manga_id('<div id="manga-chapters-holder" data-id="4711"></div>') == "4711"
    assert driver.parse_manga_id('<input class="rating-post-id" type="hidden" value="99">') == "99"
    assert driver.parse_manga_id('<script>var x = {"manga_id":"123"};</script>') == "123"
    assert driver.parse_manga_id("<html></html>") is None


async def test_list_chapters_from_inline_listing(mock_client):
    html = """
    <li class="wp-manga-chapter"><a href="/manga/solo-leveling/chapter-2/">2</a></li>
    <li class="wp-manga-chapter"><a href="/manga/solo-leveling/chapter-1/">1</a></li>
    <li class="wp-manga-chapter"><a href="/manga/solo-leveling/chapter-1/">1 again</a></li>
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        return httpx.Response(200, text=html)

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, f"{HOST}/manga/solo-leveling")

    assert chapters == [
        (f"{HOST}/manga/solo-leveling/chapter-1/", 1.0),
        (f"{HOST}/manga/solo-leveling/chapter-2/", 2.0),
    ]
    assert calls == [f"GET {LIST_URL}"]


async def test_list_chapters_falls_back_to_ajax_chapters_endpoint(mock_client):
    ajax_html = '<li class="wp-manga-chapter"><a href="/manga/solo-leveling/chapter-5/">5</a></li>'
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and str(request.url) == f"{LIST_URL}ajax/chapters/":
            return httpx.Response(200, text=ajax_html)
        return httpx.Response(200, text="<html><body>no inline list</body></html>")

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, LIST_URL)

    assert chapters == [(f"{HOST}/manga/solo-leveling/chapter-5/", 5.0)]
    post = seen[1]
    assert post.headers["X-Requested-With"] == "XMLHttpRequest"


async def test_list_chapters_falls_back_to_admin_ajax_with_manga_id(mock_client):
    series_html = '<html><body><div id="manga-chapters-holder" data-id="4711"></div></body></html>'
    admin_html = '<li class="wp-manga-chapter"><a href="/manga/solo-leveling/chapter-3/">3</a></li>'
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == f"{HOST}/wp-admin/admin-ajax.php":
            return httpx.Response(200, text=admin_html)
        if request.method == "POST":
            return httpx.Response(404)
        return httpx.Response(200, text=series_html)

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, LIST_URL)

    assert chapters == [(f"{HOST}/manga/solo-leveling/chapter-3/", 3.0)]
    admin = seen[-1]
    assert admin.method == "POST"
    assert b"action=manga_get_chapters" in admin.content and b"manga=4711" in admin.content


async def test_list_chapters_empty_when_every_source_is_empty(mock_client):
    async with mock_client(lambda r: httpx.Response(200, text="<html></html>")) as client:
        assert await driver.list_chapters(client, LIST_URL) == []


async def test_image_urls_prefers_lazy_attributes_and_resolves_relative(mock_client):
    html = """
    <html><body>
      <div class="reading-content">
        <div class="page-break"><img data-src="  https://cdn.example/1.jpg
        " src="https://cdn.example/placeholder.gif" class="wp-manga-chapter-img"></div>
        <div class="page-break"><img data-lazy-src="/images/2.webp" src="data:image/gif;base64,R0lG"></div>
        <div class="page-break"><img src="https://cdn.example/3.png"></div>
        <div class="page-break"><img src="data:image/gif;base64,R0lG"></div>
      </div>
      <img src="https://cdn.example/outside-reader.jpg">
    </body></html>
    """
    async with mock_client(lambda r: httpx.Response(200, text=html)) as client:
        urls = await driver.image_urls(client, CHAPTER_URL)

    assert urls == ["https://cdn.example/1.jpg", f"{HOST}/images/2.webp", "https://cdn.example/3.png"]
