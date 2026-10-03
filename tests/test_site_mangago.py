"""Tests for the mangago.me driver: URL classification, chapter-label
handling, and chapter-table parsing -- via httpx.MockTransport.

image_urls() itself needs a real browser to read the site's own JS-decrypted
DOM (see src/sites/mangago.py's docstring for why), so it isn't covered
here -- same reason tests/mangago_login.py is a standalone, non-pytest tool
rather than part of this hermetic suite.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import httpx
import pytest

from src.sites import mangago
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


def test_client_without_playwright_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mangago, "find_spec", lambda name: None)
    monkeypatch.setenv("MANGAGO_COOKIE", "PHPSESSID=x")
    with pytest.raises(RuntimeError, match=r"\.\[mangago\]"):
        driver.client()


def test_registry_keeps_mangago_and_a_clean_stdout_without_playwright():
    """Without the `mangago` extra the driver still registers, and importing the registry prints nothing on stdout."""
    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys; sys.modules['playwright'] = None\n"
        "import config\n"
        "sys.stderr.write(','.join(sorted(config.SUPPORTED_SITES)))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "mangago" in proc.stderr.split(",")


async def test_session_cookie_goes_to_mangago_only_never_to_the_image_cdn(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mangago, "find_spec", lambda name: object())
    monkeypatch.setenv("MANGAGO_COOKIE", "PHPSESSID=secret; other=1")
    sent: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent[request.url.host] = request.headers.get("cookie")
        return httpx.Response(200)

    async with driver.client(transport=httpx.MockTransport(handler)) as client:
        await client.get(f"{BASE}/read-manga/it_s_mine/")
        await client.get("https://iweb_3.mangapicgallery.com/r/newpiclink/it_s_mine/157/1.jpg")

    assert sent["www.mangago.me"] == "PHPSESSID=secret; other=1"
    assert sent["iweb_3.mangapicgallery.com"] is None


async def test_a_browser_that_will_not_start_does_not_leak_its_playwright_driver(monkeypatch: pytest.MonkeyPatch):
    """`playwright install firefox` never run: each chapter's attempt must stop the driver it started."""
    calls = {"start": 0, "stop": 0}

    class FakePlaywright:
        def __init__(self) -> None:
            self.firefox = self

        async def launch(self, **kwargs):
            raise RuntimeError("Executable doesn't exist at ~/.cache/ms-playwright/firefox")

        async def stop(self) -> None:
            calls["stop"] += 1

    class Starter:
        async def start(self) -> FakePlaywright:
            calls["start"] += 1
            return FakePlaywright()

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = Starter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    monkeypatch.setenv("MANGAGO_COOKIE", "PHPSESSID=x")
    fresh = MangagoDriver()

    for _ in range(2):  # two chapters, each trying to start the browser
        with pytest.raises(RuntimeError, match="Executable doesn't exist"):
            await fresh._ensure_context()

    assert calls == {"start": 2, "stop": 2}
    assert fresh._pw is None and fresh._browser is None and fresh._context is None


async def test_download_series_url_passes_a_given_listing_through(tmp_path: Path):
    """The listing main.py already fetched reaches the base facade: the list page is not requested again."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(500)

    class MockedDriver(MangagoDriver):
        def client(self, **kwargs) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    links = [(LABELED_CHAPTER_URL, 157.0)]
    with pytest.raises(RuntimeError, match="No chapters matched"):
        await MockedDriver().download_series_url(LIST_URL, tmp_path, chapters=[1], links=links)
    assert requested == []
