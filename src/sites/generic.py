"""Best-effort driver for sites without a dedicated one.

Most manga readers share one shape: a series page full of links whose URLs
carry a chapter number, and a chapter page whose images sit together in
one container (or in one JSON array inside a script). This driver bets on
exactly that and nothing site-specific, which is why it has no domains, the
lowest priority (every real driver outranks it in config.resolve_site) and
"best effort" in its name: it reads the structure, never a site's quirks.

The image heuristic is deliberately "largest group wins": a chapter page's
real pages share a parent element (or one script array), while the logos,
thumbnails and avatars that would otherwise pollute the result are
scattered around the page one at a time. Obvious non-page images are
dropped by URL before grouping so they cannot form a group of their own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from src.base import SiteDriver
from src.htmlutil import all_of, attr, parse_html

# "chapter-12", "/ch/12", "episode_7", "ep12.5", "chap-3-1" (-> 3.1): a chapter
# word at a segment/word boundary, separators, then the number.
_CHAPTER_WORD_RE = re.compile(
    r"(?:^|[/_\-.])(?:chapter|chap|ch|episode|ep)[\-_/ ]*(\d+)(?:[.\-](\d+)(?!\d))?(?!\d)", re.I
)
# A chapter word without a number ("/chapter/latest") still marks a chapter link.
_CHAPTER_HINT_RE = re.compile(r"(?:^|[/_\-.])(?:chapter|chap|ch|episode|ep)(?=[\-_/ .]|\d)", re.I)
# A final numeric segment under a series path: /manga/one-piece/1050/ (or .html).
_TRAILING_NUM_RE = re.compile(r"^/(?:[^/]+/)+(\d+)(?:\.html?)?/?$")
_TEXT_CHAPTER_RE = re.compile(r"(?:chapter|chap|ch|episode|ep)\.?\s*(\d+(?:\.\d+)?)", re.I)
_TEXT_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")

_JUNK_RE = re.compile(
    r"(?<![a-z])(?:logos?|avatars?|icons?|favicon|banners?|ads|emojis?|loading|placeholder|thumb(?:nail)?s?)(?![a-z])"
    r"|\.(?:svg|gif)(?![a-z])",
    re.I,
)
_SCRIPT_URL_RE = re.compile(r"(?:https?:)?//[^\"'\s<>\\)]+?\.(?:jpe?g|png|webp|avif|bmp)(?:\?[^\"'\s<>\\)]*)?", re.I)
_IMG_ATTRS = ("data-src", "data-lazy-src", "data-original")
_SKIP_HREF_PREFIXES = ("#", "javascript:", "mailto:", "tel:")
# Path tails that name a listing rather than the series itself.
_LISTING_WORDS = frozenset({"chapters", "chapter", "list", "read", "manga", "series", "comic", "webtoon"})


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _to_num(whole: str, frac: str | None) -> float:
    return float(f"{whole}.{frac}") if frac else float(whole)


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def is_junk_image(url: str) -> bool:
    """True for URLs that are plainly not chapter pages (logos, icons, data: URIs, ...)."""
    return url.startswith("data:") or _JUNK_RE.search(url) is not None


def largest_srcset_candidate(srcset: str) -> str:
    """The URL with the biggest width/density descriptor in a srcset (last one when unsized)."""
    best_url, best_size = "", -1.0
    for entry in srcset.split(","):
        parts = entry.strip().split()
        if not parts:
            continue
        size = 0.0
        if len(parts) > 1:
            m = _TEXT_NUM_RE.match(parts[1])
            size = float(m.group(1)) if m else 0.0
        if size >= best_size:
            best_url, best_size = parts[0], size
    return best_url


class GenericDriver(SiteDriver):
    key = "generic"
    name = "Any site (best effort)"
    domains = ()
    priority = -100

    def referer_for(self, url: str) -> str | None:
        return _origin(url) + "/"

    def sniff(self, url: str, html: str) -> bool:
        """Claims any page at all -- the lowest priority makes this the last resort."""
        return bool(html and html.strip())

    # ---- URL handling ------------------------------------------------------

    @staticmethod
    def chapter_number_from_url(url: str) -> float | None:
        """Chapter number carried by a URL path, or None when it has no chapter pattern."""
        path = unquote(urlparse(url).path)
        m = _CHAPTER_WORD_RE.search(path)
        if m:
            return _to_num(m.group(1), m.group(2))
        m = _TRAILING_NUM_RE.match(path)
        if m:
            return float(m.group(1))
        return None

    @staticmethod
    def looks_like_chapter(url: str) -> bool:
        path = unquote(urlparse(url).path)
        return bool(_CHAPTER_HINT_RE.search(path) or _TRAILING_NUM_RE.match(path))

    def classify(self, url: str) -> str:
        return "chapter" if self.chapter_number_from_url(url) is not None else "list"

    def series_slug(self, url: str) -> str:
        """Last meaningful path segment, with any chapter tail stripped off first."""
        path = unquote(urlparse(url).path)
        m = _CHAPTER_WORD_RE.search(path)
        if m:
            path = path[: m.start()]
        else:
            m = _TRAILING_NUM_RE.match(path)
            if m:
                path = path[: m.start(1)]
        segments = [s for s in path.split("/") if s]
        while len(segments) > 1 and segments[-1].lower() in _LISTING_WORDS:
            segments.pop()
        if not segments:
            return _host(url) or "series"
        return re.sub(r"\.html?$", "", segments[-1], flags=re.I)

    # ---- site specifics ----------------------------------------------------

    @staticmethod
    def chapter_label(num: float) -> str:
        if num.is_integer():
            return str(int(num))
        return f"{num:.3f}".rstrip("0").rstrip(".")

    def folder_name(self, chapter_url: str) -> str:
        num = self.chapter_number_from_url(chapter_url)
        if num is None:
            return f"num0_{self.safe('Chapter unknown')}"
        return f"num{num:g}_{self.safe(f'Chapter {self.chapter_label(num)}')}"

    async def list_chapters(self, client: httpx.AsyncClient, list_url: str) -> list[tuple[str, float]]:
        """Chapter links found on the series page, oldest first."""
        html = await self._page_html(client, list_url)
        return self.parse_chapter_links(html, list_url)

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        """The page's largest coherent group of image URLs, in document order."""
        html = await self._page_html(client, chapter_url)
        return self.parse_image_urls(html, chapter_url)

    # ---- page parsing helpers ----------------------------------------------

    @staticmethod
    async def _page_html(client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    @classmethod
    def chapter_number_from_text(cls, text: str) -> float | None:
        m = _TEXT_CHAPTER_RE.search(text) or _TEXT_NUM_RE.search(text)
        return float(m.group(1)) if m else None

    @classmethod
    def parse_chapter_links(cls, html: str, page_url: str) -> list[tuple[str, float]]:
        """Same-host links with a chapter pattern, numbered from the href (else the link text).

        Links under the series' own path are preferred over the rest of the
        page when there are any, so a "latest updates" sidebar pointing at
        other series does not leak into the listing.
        """
        doc = parse_html(html)
        host = _host(page_url)
        series_path = unquote(urlparse(page_url).path).rstrip("/")
        found: list[tuple[str, float]] = []
        for a in all_of(doc, "//a[@href]"):
            href = (attr(a, "href") or "").strip()
            if not href or href.lower().startswith(_SKIP_HREF_PREFIXES):
                continue
            url = urljoin(page_url, href).split("#", 1)[0]
            if _host(url) != host or not cls.looks_like_chapter(url):
                continue
            num = cls.chapter_number_from_url(url)
            if num is None:
                num = cls.chapter_number_from_text(" ".join(a.itertext()))
            if num is None:
                continue
            found.append((url, num))

        if series_path:
            own = [(url, num) for url, num in found if unquote(urlparse(url).path).startswith(series_path + "/")]
            if own:
                found = own

        deduped: dict[float, str] = {}
        for url, num in found:
            deduped.setdefault(num, url)
        return [(url, num) for num, url in sorted(deduped.items())]

    @staticmethod
    def _image_candidate(img) -> str:
        """One URL per <img>: lazy-load attributes, then the largest srcset entry, then src."""
        for name in _IMG_ATTRS:
            value = (attr(img, name) or "").strip()
            if value:
                return value
        srcset = (attr(img, "data-srcset") or attr(img, "srcset") or "").strip()
        if srcset:
            return largest_srcset_candidate(srcset)
        return (attr(img, "src") or "").strip()

    @staticmethod
    def _group_ancestor(img, counts: dict[Any, int]):
        """Nearest ancestor holding more than one image -- wrappers around a single image are skipped.

        Readers commonly wrap each page (`<p><img></p>`, `<a><img></a>`,
        `<noscript><img>`); grouping by the immediate parent would split one
        chapter into dozens of one-image groups. The dicts are keyed by the
        element objects themselves (not id()): lxml hands out transient
        proxies whose id() is recycled once they are collected, and holding
        them as keys is what keeps their identity stable.
        """
        node = img.getparent()
        while node is not None:
            if node not in counts:
                counts[node] = sum(1 for _ in node.iter("img"))
            if counts[node] > 1:
                return node
            node = node.getparent()
        return None

    @classmethod
    def dom_image_groups(cls, doc, page_url: str) -> list[list[str]]:
        """Image URL groups keyed by shared container, each in document order."""
        groups: dict[Any, list[str]] = {}
        counts: dict[Any, int] = {}
        for img in all_of(doc, "//img"):
            raw = cls._image_candidate(img)
            if not raw or raw.startswith("data:"):
                continue
            url = urljoin(page_url, raw)
            if is_junk_image(url):
                continue
            ancestor = cls._group_ancestor(img, counts)
            groups.setdefault(ancestor, []).append(url)
        return [_dedupe(urls) for urls in groups.values()]

    @staticmethod
    def script_image_lists(doc, page_url: str) -> list[list[str]]:
        """Image URL lists found in each <script>'s text (JSON-escaped slashes unescaped)."""
        lists: list[list[str]] = []
        for script in all_of(doc, "//script"):
            text = (script.text or "").replace("\\/", "/")
            if not text:
                continue
            urls = [urljoin(page_url, m.group(0)) for m in _SCRIPT_URL_RE.finditer(text)]
            urls = _dedupe(u for u in urls if not is_junk_image(u))
            if urls:
                lists.append(urls)
        return lists

    @classmethod
    def parse_image_urls(cls, html: str, page_url: str) -> list[str]:
        """Largest DOM image group or largest script list (DOM wins ties), de-duplicated."""
        doc = parse_html(html)
        if doc is None:
            return []
        dom_best = max(cls.dom_image_groups(doc, page_url), key=len, default=[])
        script_best = max(cls.script_image_lists(doc, page_url), key=len, default=[])
        return dom_best if len(dom_best) >= len(script_best) else script_best


driver = GenericDriver()
