"""mangago.me site driver.

Needs a logged-in session (see README.md "Why mangago needs an account" and
tests/mangago_login.py) for two reasons:

1. The chapter's page images aren't in the HTML as plain <img src>. They're
   in a `var imgsrcs = '...'` blob, AES-encrypted client-side (a bundled,
   obfuscated CryptoJS build in chapter_ini.js does the decrypting) and only
   decoded into real <img src> attributes by that JS actually running. Rather
   than reverse-engineer a deliberately obfuscated, changeable cipher, this
   drives a real (headless) Playwright browser to the chapter page and reads
   the DOM after the site's own JS has decrypted it -- robust to that cipher
   changing, since nothing here depends on how it works.
2. Anonymous readers get that treatment one panel-page at a time (a fresh
   page load per panel); logged in, one page load exposes every panel of the
   chapter at once (each <img class="pageN">), which is what makes this
   worth doing at all.

The resulting CDN image URLs (mangapicgallery.com) are plain, unauthenticated
and unsigned -- confirmed by fetching one with plain httpx, no cookie or
Referer needed -- so only *finding* the URLs needs a browser; the actual
image bytes still go through the normal httpx-based download engine.

The chapter list itself is plain HTML (no encryption), so list_chapters is
ordinary httpx + lxml, same shape as the other drivers.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright._impl._api_structures import SetCookieParam
from playwright.async_api import Browser, BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError

from src.base import SiteDriver
from src.htmlutil import all_of, attr, has_class, parse_html, stripped_text
from term import cwarning

BASE = "https://www.mangago.me"

_SLUG_RE = re.compile(r"^/read-manga/([^/]+)/?$")
_CHAPTER_RE = re.compile(r"^/read-manga/([^/]+)/uu/([^/]+)/pg-\d+/?$")
_ROW_XPATH = f"//table[@id='chapter_table']//a[{has_class('chico')}]"
_LABEL_RE = re.compile(r"Ch\.?\s*([\d]+(?:\.[\d]+)?)", re.IGNORECASE)
_LABEL_FRAG_RE = re.compile(r"#([\d.]+)$")
_PAGE_CLASS_RE = re.compile(r"page(\d+)$")

# Waits for the site's own JS to finish decrypting `imgsrcs` into real <img
# src> values -- deterministic (checks the actual outcome) instead of a
# networkidle guess, which is unreliable on a page that also loads ads.
_PAGES_READY_JS = """() => {
    const imgs = document.querySelectorAll('img[class^="page"]');
    return typeof total_pages !== 'undefined'
        && imgs.length > 0
        && imgs.length === total_pages
        && Array.from(imgs).every(img => img.src && img.naturalWidth > 0);
}"""


def _strip_label(url: str) -> str:
    return url.split("#", 1)[0]


def _extract_label(url: str) -> str | None:
    m = _LABEL_FRAG_RE.search(url)
    return m.group(1) if m else None


def _parse_cookie_header(cookie: str) -> list[SetCookieParam]:
    cookies: list[SetCookieParam] = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({"name": name.strip(), "value": value.strip(), "domain": ".mangago.me", "path": "/"})
    return cookies


class MangagoDriver(SiteDriver):
    key = "mangago"
    name = "mangago.me"
    domains = ("mangago.me",)
    referer = BASE + "/"

    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._browser_lock = asyncio.Lock()

    def base_url(self) -> str:
        return BASE

    @property
    def extra_headers(self) -> dict[str, str] | None:
        cookie = os.environ.get("MANGAGO_COOKIE")
        return {"Cookie": cookie} if cookie else None

    def client(self, **kwargs) -> httpx.AsyncClient:
        if not os.environ.get("MANGAGO_COOKIE"):
            raise RuntimeError(
                "MANGAGO_COOKIE is not set -- run `python -m tests.mangago_login` first "
                '(see README.md "Why mangago needs an account").'
            )
        # httpx's HTTP/2 stack gets a flat 403 from mangago's Cloudflare bot
        # management regardless of headers/cookie -- verified by isolating it
        # against plain HTTP/1.1 with the same headers, which is unaffected.
        # Same family of TLS/protocol fingerprinting as the login flow
        # (see tests/mangago_login.py), just satisfied here by turning HTTP/2
        # off instead of needing a full browser.
        kwargs.setdefault("http2", False)
        return super().client(**kwargs)

    # ---- URL handling -------------------------------------------------------

    def is_chapter_url(self, url: str) -> bool:
        return _CHAPTER_RE.match(urlparse(_strip_label(url)).path) is not None

    def is_list_url(self, url: str) -> bool:
        return _SLUG_RE.match(urlparse(_strip_label(url)).path) is not None

    def classify(self, url: str) -> str:
        if self.is_chapter_url(url):
            return "chapter"
        if self.is_list_url(url):
            return "list"
        return "unknown"

    def series_slug(self, url: str) -> str:
        path = urlparse(_strip_label(url)).path
        m = _CHAPTER_RE.match(path) or _SLUG_RE.match(path)
        if not m:
            raise ValueError(f"Could not extract series slug from {url}")
        return m.group(1)

    # ---- site specifics -------------------------------------------------

    def folder_name(self, chapter_url: str) -> str:
        label = _extract_label(chapter_url)
        if label is not None:
            try:
                num = float(label)
            except ValueError:
                num = 0.0
            return f"num{num:g}_{self.safe(f'Chapter {label}')}"
        # A bare chapter URL with no label attached (e.g. the user pasted a
        # single-chapter link directly, so list_chapters -- the only place a
        # label gets attached -- never ran): fall back to the opaque
        # internal chapter slug from the URL, at least unique and stable.
        path = urlparse(_strip_label(chapter_url)).path
        m = _CHAPTER_RE.match(path)
        slug = m.group(2) if m else path.rsplit("/", 1)[-1]
        return f"num0_{self.safe(slug)}"

    async def list_chapters(self, client: httpx.AsyncClient, list_url: str) -> list[tuple[str, float]]:
        """Return [(chapter_url, num), ...] sorted oldest -> newest.

        Each returned url carries its chapter label as a `#<label>` fragment
        (stripped before any real request -- see _strip_label) since
        folder_name() only receives the url, and mangago's own chapter-url
        id (to_chapter-157, nhs_chapter-855550, ...) is an opaque internal
        id, not the displayed chapter number.
        """
        resp = await client.get(_strip_label(list_url))
        resp.raise_for_status()
        doc = parse_html(resp.text)

        deduped: dict[float, str] = {}
        for a in all_of(doc, _ROW_XPATH):
            href = attr(a, "href") or ""
            if not href:
                continue
            m = _LABEL_RE.search(stripped_text(a))
            if not m:
                continue
            label = m.group(1)
            try:
                num = float(label)
            except ValueError:
                continue
            deduped.setdefault(num, f"{href}#{label}")

        return [(url, num) for num, url in sorted(deduped.items())]

    async def _ensure_context(self) -> BrowserContext:
        async with self._browser_lock:
            if self._context is None:
                cookie = os.environ["MANGAGO_COOKIE"]  # client() already required this
                self._pw = await async_playwright().start()
                self._browser = await self._pw.firefox.launch(headless=True)
                self._context = await self._browser.new_context()
                await self._context.add_cookies(_parse_cookie_header(cookie))
            return self._context

    async def _close_browser(self) -> None:
        async with self._browser_lock:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
            self._browser = None
            self._context = None
            self._pw = None

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        context = await self._ensure_context()
        page = await context.new_page()
        try:
            await page.goto(_strip_label(chapter_url), wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_function(_PAGES_READY_JS, timeout=30000)
            pairs = await page.eval_on_selector_all("img[class^='page']", "els => els.map(e => [e.className, e.src])")
        except PlaywrightError as exc:
            cwarning(f"  mangago: failed to load {chapter_url}: {exc}")
            return []
        finally:
            await page.close()

        def _page_num(cls: str) -> int:
            m = _PAGE_CLASS_RE.match(cls)
            return int(m.group(1)) if m else 0

        ordered = sorted((p for p in pairs if p[1]), key=lambda p: _page_num(p[0]))
        return [src for _, src in ordered]

    async def download_series_url(
        self,
        url: str,
        out_dir: Path,
        chapters: list[int] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        try:
            return await super().download_series_url(url, out_dir, chapters, dry_run)
        finally:
            await self._close_browser()


driver = MangagoDriver()
