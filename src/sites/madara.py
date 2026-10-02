"""Madara (WordPress theme) site driver.

Madara is the WordPress theme behind hundreds of manga/manhwa sites, all
sharing the same markup: `li.wp-manga-chapter` entries on the series page
and `div.reading-content img` on the reader page. One driver therefore
covers them all -- it has no fixed domains and is picked by content sniffing
(see config.resolve_site) when a URL's host matches no dedicated driver.

The chapter list has moved around across theme versions, so listing is a
cascade: the series page itself (old themes render the list inline), then
`<series>/ajax/chapters/` (newer themes load it with an XHR POST), then the
classic `admin-ajax.php?action=manga_get_chapters` endpoint keyed by the
post id found on the page. Images are lazy-loaded, so `data-src` is
preferred over the placeholder `src`.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx

from src.base import SiteDriver
from src.htmlutil import all_of, attr, first, has_class, parse_html

AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

# chapter-12 / chapter-12-5 (-> 12.5) / ch-12 / chap-12, optionally followed
# by a title tail the theme appends ("chapter-12-the-return").
_CHAPTER_SEG_RE = re.compile(r"^(?:chapter|chap|ch)-(\d+)(?:-(\d+)(?!\d))?(?:-[^/]*)?$", re.I)
_SNIFF_RE = re.compile(r"wp-manga|madara-core|manga_id|\bmadara[-_./]", re.I)
_BODY_CLASS_RE = re.compile(r"<body\b[^>]*\bclass\s*=\s*[\"'][^\"']*\bmanga-page\b", re.I)
_MANGA_ID_JS_RE = re.compile(r"manga_id[\"']?\s*[:=]\s*[\"']?(\d+)")


def _segments(url: str) -> list[str]:
    return [p for p in urlparse(url).path.split("/") if p]


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


class MadaraDriver(SiteDriver):
    key = "madara"
    name = "Madara (WordPress theme sites)"
    domains = ()
    priority = 10

    def referer_for(self, url: str) -> str | None:
        return _origin(url) + "/"

    def sniff(self, url: str, html: str) -> bool:
        """Claim a page that carries the theme's class names, plugin path or body class."""
        return bool(_SNIFF_RE.search(html) or _BODY_CLASS_RE.search(html))

    # ---- URL handling ------------------------------------------------------

    @staticmethod
    def parse_chapter_url(url: str) -> tuple[str, str, float] | None:
        """(prefix, series slug, chapter number) for /<prefix>/<slug>/chapter-N[-F]/[page/]."""
        parts = _segments(url)
        if len(parts) == 4 and parts[3].isdigit():
            parts = parts[:3]
        if len(parts) != 3:
            return None
        m = _CHAPTER_SEG_RE.match(parts[2])
        if not m:
            return None
        whole, frac = m.group(1), m.group(2)
        num = float(f"{whole}.{frac}") if frac else float(whole)
        return parts[0], parts[1], num

    def is_chapter_url(self, url: str) -> bool:
        return self.parse_chapter_url(url) is not None

    def is_list_url(self, url: str) -> bool:
        return len(_segments(url)) == 2

    def classify(self, url: str) -> str:
        if self.is_chapter_url(url):
            return "chapter"
        if self.is_list_url(url):
            return "list"
        return "unknown"

    def series_slug(self, url: str) -> str:
        parts = _segments(url)
        if len(parts) >= 2:
            return parts[1]
        raise ValueError(f"Could not extract series slug from {url}")

    def series_url(self, url: str) -> str:
        """Canonical series page URL (trailing slash, as the ajax endpoint hangs off it)."""
        parts = _segments(url)
        if len(parts) < 2:
            raise ValueError(f"Could not extract series from {url}")
        return f"{_origin(url)}/{parts[0]}/{parts[1]}/"

    # ---- site specifics ----------------------------------------------------

    @staticmethod
    def chapter_num_from_url(url: str) -> float | None:
        parsed = MadaraDriver.parse_chapter_url(url)
        return parsed[2] if parsed else None

    @staticmethod
    def chapter_label(num: float) -> str:
        if num.is_integer():
            return str(int(num))
        return f"{num:.3f}".rstrip("0").rstrip(".")

    def folder_name(self, chapter_url: str) -> str:
        num = self.chapter_num_from_url(chapter_url)
        if num is None:
            return f"num0_{self.safe('Chapter unknown')}"
        return f"num{num:g}_{self.safe(f'Chapter {self.chapter_label(num)}')}"

    async def list_chapters(self, client: httpx.AsyncClient, list_url: str) -> list[tuple[str, float]]:
        """Return [(chapter_url, num), ...] oldest first, trying each listing mechanism in turn."""
        series_url = self.series_url(list_url)
        html = await self._page_html(client, series_url)
        chapters = self.parse_chapter_links(html, series_url)

        if not chapters:
            resp = await client.post(series_url + "ajax/chapters/", headers=AJAX_HEADERS)
            if resp.is_success:
                chapters = self.parse_chapter_links(resp.text, series_url)

        if not chapters:
            manga_id = self.parse_manga_id(html)
            if manga_id:
                resp = await client.post(
                    f"{_origin(series_url)}/wp-admin/admin-ajax.php",
                    data={"action": "manga_get_chapters", "manga": manga_id},
                    headers=AJAX_HEADERS,
                )
                if resp.is_success:
                    chapters = self.parse_chapter_links(resp.text, series_url)

        deduped: dict[float, str] = {}
        for url, num in chapters:
            deduped.setdefault(num, url)
        return [(url, num) for num, url in sorted(deduped.items())]

    async def image_urls(self, client: httpx.AsyncClient, chapter_url: str) -> list[str]:
        """Reader-page images in order (lazy-load attributes preferred over the placeholder src)."""
        html = await self._page_html(client, chapter_url)
        return self.parse_image_urls(html, chapter_url)

    # ---- page parsing helpers ----------------------------------------------

    @staticmethod
    async def _page_html(client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    @classmethod
    def parse_chapter_links(cls, html: str, base_url: str) -> list[tuple[str, float]]:
        """Extract (chapter_url, num) pairs from li.wp-manga-chapter entries, in page order."""
        doc = parse_html(html)
        out: list[tuple[str, float]] = []
        for a in all_of(doc, f"//li[{has_class('wp-manga-chapter')}]//a[@href]"):
            href = (attr(a, "href") or "").strip()
            if not href:
                continue
            url = urljoin(base_url, href)
            parsed = cls.parse_chapter_url(url)
            if parsed is None:
                continue
            out.append((url, parsed[2]))
        return out

    @staticmethod
    def parse_manga_id(html: str) -> str | None:
        """The WordPress post id the admin-ajax chapter endpoint is keyed by."""
        doc = parse_html(html)
        holder = first(doc, "//*[@id='manga-chapters-holder'][@data-id]")
        if holder is None:
            holder = first(doc, f"//*[{has_class('wp-manga-chapter-holder')}][@data-id]")
        manga_id = (attr(holder, "data-id") or "").strip()
        if manga_id:
            return manga_id
        rating = first(doc, f"//input[{has_class('rating-post-id')}][@value]")
        manga_id = (attr(rating, "value") or "").strip()
        if manga_id:
            return manga_id
        m = _MANGA_ID_JS_RE.search(html)
        return m.group(1) if m else None

    @staticmethod
    def parse_image_urls(html: str, page_url: str) -> list[str]:
        """Ordered image URLs under div.reading-content."""
        doc = parse_html(html)
        urls: list[str] = []
        for img in all_of(doc, f"//div[{has_class('reading-content')}]//img"):
            candidates = ((attr(img, name) or "").strip() for name in ("data-src", "data-lazy-src", "src"))
            src = next((c for c in candidates if c), "")
            if not src or src.startswith("data:"):
                continue
            urls.append(urljoin(page_url, src))
        return urls


driver = MadaraDriver()
