"""Tests for the mangago.me driver: URL classification, chapter-label
handling, and chapter-table parsing -- via httpx.MockTransport.

image_urls() itself needs a real browser to read the site's own JS-decrypted
DOM (see src/sites/mangago.py's docstring for why), so it isn't covered
here -- same reason tests/mangago_login.py is a standalone, non-pytest tool
rather than part of this hermetic suite.
"""

from __future__ import annotations

import httpx

from src.sites.mangago import BASE, MangagoDriver, _extract_label, _parse_cookie_header, _strip_label

driver = MangagoDriver()

LIST_URL = f"{BASE}/read-manga/it_s_mine/"
CHAPTER_URL = f"{BASE}/read-manga/it_s_mine/uu/to_chapter-157/pg-1/"
LABELED_CHAPTER_URL = f"{CHAPTER_URL}#157"

# Real chapter ids: "to_chapter-157" happens to equal its display number, but
# "nhs_chapter-855548" (an older chapter, real number 4) proves the id is an
# opaque internal one that can't be trusted for numbering/folder names.
CHAPTER_TABLE_HTML = """
<table class="listing" id="chapter_table"><tbody>
<tr><td><h4><a class="chico"
  href="https://www.mangago.me/read-manga/it_s_mine/uu/to_chapter-157/pg-1/"><b>Ch.157</b> </a></h4></td></tr>
<tr><td><h4><a class="chico"
  href="https://www.mangago.me/read-manga/it_s_mine/uu/to_chapter-156/pg-1/"><b>Ch.156</b> </a></h4></td></tr>
<tr><td><h4><a class="chico"
  href="https://www.mangago.me/read-manga/it_s_mine/uu/nhs_chapter-855548/pg-1/"><b>Ch.4</b> </a></h4></td></tr>
</tbody></table>
"""


def test_classify_chapter_and_list_urls():
    assert driver.classify(CHAPTER_URL) == "chapter"
    assert driver.classify(LIST_URL) == "list"
    assert driver.classify(f"{BASE}/search?q=x") == "unknown"


def test_classify_ignores_label_fragment():
    assert driver.classify(LABELED_CHAPTER_URL) == "chapter"


def test_series_slug_from_list_and_chapter_urls():
    assert driver.series_slug(LIST_URL) == "it_s_mine"
    assert driver.series_slug(CHAPTER_URL) == "it_s_mine"


def test_strip_and_extract_label():
    assert _strip_label(LABELED_CHAPTER_URL) == CHAPTER_URL
    assert _extract_label(LABELED_CHAPTER_URL) == "157"
    assert _extract_label(CHAPTER_URL) is None


def test_folder_name_uses_label_not_internal_url_id():
    url = f"{BASE}/read-manga/it_s_mine/uu/nhs_chapter-855548/pg-1/#4"
    assert driver.folder_name(url) == "num4_Chapter 4"


def test_folder_name_falls_back_to_url_id_without_a_label():
    assert driver.folder_name(CHAPTER_URL) == "num0_to_chapter-157"


def test_parse_cookie_header_splits_multiple_pairs():
    cookies = _parse_cookie_header("PHPSESSID=abc123; other=value")
    assert cookies == [
        {"name": "PHPSESSID", "value": "abc123", "domain": ".mangago.me", "path": "/"},
        {"name": "other", "value": "value", "domain": ".mangago.me", "path": "/"},
    ]


async def test_list_chapters_dedupes_labels_ids_and_sorts_oldest_first(mock_client):
    async with mock_client(lambda r: httpx.Response(200, text=CHAPTER_TABLE_HTML)) as client:
        chapters = await driver.list_chapters(client, LIST_URL)

    assert [num for _, num in chapters] == [4.0, 156.0, 157.0]
    by_num = {num: url for url, num in chapters}
    assert by_num[157.0] == f"{CHAPTER_URL}#157"
