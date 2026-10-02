"""Tests for the best-effort generic driver's heuristics, on hand-written
HTML: chapter URL patterns, chapter-link harvesting, the largest-group image
heuristic (DOM groups, srcset, noscript, script arrays, junk filtering),
folder and slug derivation -- fetches via httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest

from src.sites.generic import GenericDriver, is_junk_image, largest_srcset_candidate

driver = GenericDriver()
HOST = "https://reader.example"


def test_identity_is_the_lowest_priority_catch_all():
    assert driver.domains == ()
    assert driver.priority < 0
    assert driver.sniff(f"{HOST}/x", "<html><body>anything</body></html>")
    assert not driver.sniff(f"{HOST}/x", "   ")


@pytest.mark.parametrize(
    ("url", "kind", "num"),
    [
        (f"{HOST}/manga/one-piece/chapter-12/", "chapter", 12.0),
        (f"{HOST}/manga/one-piece/chapter-12-5/", "chapter", 12.5),
        (f"{HOST}/manga/one-piece/chap_3", "chapter", 3.0),
        (f"{HOST}/manga/one-piece/ch/7/", "chapter", 7.0),
        (f"{HOST}/series/x/episode-9", "chapter", 9.0),
        (f"{HOST}/ep12.5", "chapter", 12.5),
        (f"{HOST}/manga/one-piece/1050/", "chapter", 1050.0),
        (f"{HOST}/read/one-piece/1050.html", "chapter", 1050.0),
        (f"{HOST}/manga/one-piece/chapter-12/3/", "chapter", 12.0),
        (f"{HOST}/manga/one-piece/", "list", None),
        (f"{HOST}/1050/", "list", None),
        (f"{HOST}/series/search-results/", "list", None),
        (f"{HOST}/comic/watch-me/", "list", None),
        (f"{HOST}/comic/epic-tale/", "list", None),
    ],
)
def test_classify_and_chapter_number(url: str, kind: str, num: float | None):
    assert driver.classify(url) == kind
    assert driver.chapter_number_from_url(url) == num


def test_series_slug_strips_chapter_tails_and_listing_words():
    assert driver.series_slug(f"{HOST}/manga/one-piece/") == "one-piece"
    assert driver.series_slug(f"{HOST}/manga/one-piece/chapter-12/") == "one-piece"
    assert driver.series_slug(f"{HOST}/manga/one-piece/1050/") == "one-piece"
    assert driver.series_slug(f"{HOST}/read/one-piece-chapter-12") == "one-piece"
    assert driver.series_slug(f"{HOST}/manga/one-piece/chapters/") == "one-piece"
    assert driver.series_slug(f"{HOST}/series/one-piece.html") == "one-piece"
    assert driver.series_slug(f"{HOST}/") == "reader.example"


def test_folder_name():
    assert driver.folder_name(f"{HOST}/manga/x/chapter-12/") == "num12_Chapter 12"
    assert driver.folder_name(f"{HOST}/manga/x/chapter-12-5/") == "num12.5_Chapter 12.5"
    assert driver.folder_name(f"{HOST}/manga/x/") == "num0_Chapter unknown"


def test_referer_is_the_page_origin():
    assert driver.referer_for(f"{HOST}/manga/x/chapter-1/") == f"{HOST}/"


def test_parse_chapter_links_same_host_numbers_and_text_fallback():
    html = f"""
    <html><body>
      <a href="/manga/one-piece/chapter-2/">Chapter 2</a>
      <a href="chapter-1/">Chapter 1</a>
      <a href="{HOST}/manga/one-piece/chapter-1/#comments">Chapter 1 (dup)</a>
      <a href="/manga/one-piece/chapter/">Chapter 2.5</a>
      <a href="https://other.example/manga/one-piece/chapter-3/">elsewhere</a>
      <a href="/manga/one-piece/">series</a>
      <a href="javascript:void(0)">menu</a>
    </body></html>
    """
    links = driver.parse_chapter_links(html, f"{HOST}/manga/one-piece/")
    assert links == [
        (f"{HOST}/manga/one-piece/chapter-1/", 1.0),
        (f"{HOST}/manga/one-piece/chapter-2/", 2.0),
        (f"{HOST}/manga/one-piece/chapter/", 2.5),
    ]


def test_parse_chapter_links_prefers_links_under_the_series_path():
    html = """
    <div class="chapters">
      <a href="/manga/one-piece/chapter-1/">1</a>
      <a href="/manga/one-piece/chapter-2/">2</a>
    </div>
    <div class="sidebar"><a href="/manga/naruto/chapter-700/">Latest: Naruto 700</a></div>
    """
    nums = [n for _, n in driver.parse_chapter_links(html, f"{HOST}/manga/one-piece/")]
    assert nums == [1.0, 2.0]
    # Without a series path to anchor on, everything on the host counts.
    nums = [n for _, n in driver.parse_chapter_links(html, f"{HOST}/")]
    assert nums == [1.0, 2.0, 700.0]


def test_junk_filter_uses_word_boundaries():
    assert is_junk_image("https://x/logo.png")
    assert is_junk_image("https://x/img/favicon.ico")
    assert is_junk_image("https://x/ads/banner-1.jpg")
    assert is_junk_image("https://x/a.svg")
    assert is_junk_image("https://x/loading.gif")
    assert is_junk_image("data:image/png;base64,xx")
    assert is_junk_image("https://x/thumbnails/1.jpg")
    assert not is_junk_image("https://x/wp-content/uploads/2024/01/001.jpg")
    assert not is_junk_image("https://x/catalogo/001.jpg")
    assert not is_junk_image("https://x/silicon/001.jpg")


def test_largest_srcset_candidate():
    assert largest_srcset_candidate("a.jpg 480w, b.jpg 1200w, c.jpg 800w") == "b.jpg"
    assert largest_srcset_candidate("a.jpg 1x, b.jpg 2x") == "b.jpg"
    assert largest_srcset_candidate("a.jpg, b.jpg") == "b.jpg"


def test_parse_image_urls_keeps_the_largest_dom_group_in_order():
    html = f"""
    <html><body>
      <header><img src="/logo.png"><img src="/static/avatar-1.jpg"></header>
      <div class="related"><img src="/thumbs/a.jpg"><img src="/covers/b.jpg"></div>
      <div id="reader">
        <p><img src="/pages/placeholder.png" data-src="/pages/001.jpg"></p>
        <p><img data-lazy-src="{HOST}/pages/002.jpg"></p>
        <p><img srcset="/pages/003-small.jpg 400w, /pages/003.jpg 1200w"></p>
        <p><img data-original="/pages/004.webp"></p>
        <noscript><img src="/pages/001.jpg"></noscript>
        <p><img src="/pages/005.jpg"></p>
      </div>
      <footer><img src="data:image/gif;base64,R0lG"></footer>
    </body></html>
    """
    assert driver.parse_image_urls(html, f"{HOST}/manga/x/chapter-1/") == [
        f"{HOST}/pages/001.jpg",
        f"{HOST}/pages/002.jpg",
        f"{HOST}/pages/003.jpg",
        f"{HOST}/pages/004.webp",
        f"{HOST}/pages/005.jpg",
    ]


def test_parse_image_urls_uses_script_array_when_it_is_larger():
    html = """
    <html><body>
      <div id="reader"><img src="https://cdn.example/preview/001.jpg"><img src="https://cdn.example/preview/002.jpg"></div>
      <script>var cfg = {"logo": "https:\\/\\/cdn.example\\/logo.png"};</script>
      <script>
        var pages = ["https:\\/\\/cdn.example\\/full\\/001.jpg","https:\\/\\/cdn.example\\/full\\/002.jpg",
                     "https:\\/\\/cdn.example\\/full\\/003.jpg?x=1","//cdn.example/full/004.webp",
                     "https:\\/\\/cdn.example\\/full\\/001.jpg"];
      </script>
    </body></html>
    """
    assert driver.parse_image_urls(html, "https://reader.example/manga/x/chapter-1/") == [
        "https://cdn.example/full/001.jpg",
        "https://cdn.example/full/002.jpg",
        "https://cdn.example/full/003.jpg?x=1",
        "https://cdn.example/full/004.webp",
    ]


def test_parse_image_urls_dom_wins_ties_and_empty_page_gives_nothing():
    html = '<div><img src="/a.jpg"><img src="/b.jpg"></div><script>["https://x/1.jpg","https://x/2.jpg"]</script>'
    assert driver.parse_image_urls(html, f"{HOST}/c/1/") == [f"{HOST}/a.jpg", f"{HOST}/b.jpg"]
    assert driver.parse_image_urls("", f"{HOST}/c/1/") == []
    assert driver.parse_image_urls("<html><body><p>text only</p></body></html>", f"{HOST}/c/1/") == []


async def test_list_chapters_and_image_urls_fetch_the_page(mock_client):
    list_html = '<a href="/manga/x/chapter-1/">1</a><a href="/manga/x/chapter-2/">2</a>'
    chapter_html = '<div><img src="/p/1.jpg"><img src="/p/2.jpg"></div>'

    def handler(request: httpx.Request) -> httpx.Response:
        if "chapter-1" in str(request.url):
            return httpx.Response(200, text=chapter_html)
        return httpx.Response(200, text=list_html)

    async with mock_client(handler) as client:
        chapters = await driver.list_chapters(client, f"{HOST}/manga/x/")
        images = await driver.image_urls(client, chapters[0][0])

    assert [n for _, n in chapters] == [1.0, 2.0]
    assert images == [f"{HOST}/p/1.jpg", f"{HOST}/p/2.jpg"]
