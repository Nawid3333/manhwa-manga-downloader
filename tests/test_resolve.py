"""Tests for config.resolve_site: domain match first (no fetch), then one
page fetch offered to every driver's sniff() in descending priority, and
None when the page cannot be fetched at all."""

from __future__ import annotations

import httpx
import pytest

import config

MADARA_HTML = '<html><body class="manga-page"><li class="wp-manga-chapter"><a href="x">1</a></li></body></html>'
PLAIN_HTML = "<html><body><a href='/manga/x/chapter-1/'>1</a></body></html>"


async def _never_fetch(url: str) -> str:
    raise AssertionError(f"unexpected fetch of {url}")


def test_site_for_url_still_matches_by_domain_only():
    site = config.site_for_url("https://mangadex.org/title/8f3e1818-a015-491d-bd81-3addc4d7d56a")
    assert site is not None and site.key == "mangadex"
    assert config.site_for_url("https://unknown.example/manga/x/") is None


def test_sniff_order_is_descending_priority():
    ranked = sorted(config.SUPPORTED_SITES.values(), key=lambda s: s.driver.priority, reverse=True)
    assert ranked[0].key == "madara"
    assert ranked[-1].key == "generic"


async def test_resolve_site_domain_match_does_not_fetch():
    site = await config.resolve_site("https://mgread.io/manga/one-piece", fetch_html=_never_fetch)
    assert site is not None and site.key == "mgread"


async def test_resolve_site_sniffs_madara_before_generic():
    async def fetch(url: str) -> str:
        return MADARA_HTML

    site = await config.resolve_site("https://some-madara-site.example/manga/x/", fetch_html=fetch)
    assert site is not None and site.key == "madara"


async def test_resolve_site_falls_back_to_generic_for_unknown_markup():
    async def fetch(url: str) -> str:
        return PLAIN_HTML

    site = await config.resolve_site("https://unknown.example/manga/x/", fetch_html=fetch)
    assert site is not None and site.key == "generic"


async def test_resolve_site_is_none_when_the_page_cannot_be_fetched():
    async def fetch(url: str) -> str:
        raise httpx.ConnectError("no route", request=httpx.Request("GET", url))

    assert await config.resolve_site("https://down.example/manga/x/", fetch_html=fetch) is None

    async def fetch_403(url: str) -> str:
        raise httpx.HTTPStatusError("403", request=httpx.Request("GET", url), response=httpx.Response(403))

    assert await config.resolve_site("https://blocked.example/manga/x/", fetch_html=fetch_403) is None


async def test_default_fetch_uses_the_plain_client_and_follows_redirects(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/old/":
            return httpx.Response(301, headers={"Location": "https://moved.example/manga/x/"})
        return httpx.Response(200, text=MADARA_HTML)

    def fake_client(base: str, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=kwargs["follow_redirects"])

    monkeypatch.setattr(config, "_plain_client", fake_client)
    site = await config.resolve_site("https://moved.example/old/")
    assert site is not None and site.key == "madara"
    assert seen == ["https://moved.example/old/", "https://moved.example/manga/x/"]
