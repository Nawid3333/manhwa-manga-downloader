"""Shared lxml parsing helpers for the site drivers.

BeautifulSoup was removed in favour of plain lxml + XPath. lxml was already a
hard requirement (it is the parser bs4 was wrapping), and the sibling
scrapers measured the soup layer at ~6x the cost of plain lxml per page
(16.8ms -> 2.8ms) -- paid on the event loop, where it blocks every concurrent
fetch in flight. There is now one parser and one tree type per run.
"""

from __future__ import annotations

import lxml.etree
import lxml.html


def has_class(name: str) -> str:
    """XPath predicate matching one whitespace-delimited class token.

    ``contains(@class, 'seen')`` would also match ``unseen`` -- exactly the
    kind of near-miss that silently picks the wrong element -- so class tests
    always go through this.
    """
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def parse_html(html: str):
    """Parse HTML into an lxml tree; empty or unparseable input yields None.

    Callers rely on getting None (not an exception) for blank bodies, the
    same contract trakGrab's parse_tracks uses for its pagination feeds.
    """
    if not html or not html.strip():
        return None
    try:
        return lxml.html.fromstring(html)
    except lxml.etree.ParserError:
        return None


def first(doc, xpath: str):
    """First node matching ``xpath``, or None -- lxml's select_one."""
    if doc is None:
        return None
    found = doc.xpath(xpath)
    return found[0] if found else None


def all_of(doc, xpath: str) -> list:
    """Every node matching ``xpath``; empty list for a None doc."""
    if doc is None:
        return []
    return doc.xpath(xpath)


def stripped_text(el) -> str:
    """lxml equivalent of BeautifulSoup's get_text(strip=True)."""
    return "".join(el.itertext()).strip()


def spaced_text(el) -> str:
    """lxml equivalent of get_text(" ", strip=True).

    Joining with nothing glues "Harry Potter<small>Specials</small>" into one
    word; the separator is the whole point of this variant.
    """
    return " ".join(t.strip() for t in el.itertext() if t.strip())
