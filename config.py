"""Site registry and shared configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = ROOT_DIR / "downloads"
LOGS_DIR = ROOT_DIR / "logs"


@dataclass(frozen=True)
class Site:
    """A supported manga/manhwa site."""

    key: str
    name: str
    domains: tuple[str, ...]
    list_path: str
    chapter_path: str
    module: str  # src.site.<module_name>


SUPPORTED_SITES: dict[str, Site] = {
    "wfwf504": Site(
        key="wfwf504",
        name="wfwf504.com (늑대닷컴)",
        domains=("wfwf504.com",),
        list_path="/list",
        chapter_path="/view",
        module="src.sites.wfwf",
    ),
    "nelomanga": Site(
        key="nelomanga",
        name="nelomanga.net (MangaNelo)",
        domains=("nelomanga.net",),
        list_path="/manga/",
        chapter_path="/manga/",
        module="src.sites.nelomanga",
    ),
    "mgread": Site(
        key="mgread",
        name="mgread.io",
        domains=("mgread.io",),
        list_path="/manga/",
        chapter_path="/manga/",
        module="src.sites.mgread",
    ),
}


def site_for_url(url: str) -> Site | None:
    """Return the registered site for a URL, or None."""
    lowered = url.lower()
    for site in SUPPORTED_SITES.values():
        if any(domain in lowered for domain in site.domains):
            return site
    return None


def classify_url(url: str, site: Site) -> str:
    """Return 'list', 'chapter', or 'unknown' for a URL under a known site."""
    from urllib.parse import urlparse

    # Site modules with path-based classification expose is_list_url/
    # is_chapter_url; wfwf504 keeps its prefix-based scheme.
    try:
        import importlib

        mod = importlib.import_module(site.module)
    except Exception:
        mod = None
    if mod is not None and hasattr(mod, "is_list_url") and hasattr(mod, "is_chapter_url"):
        if mod.is_chapter_url(url):
            return "chapter"
        if mod.is_list_url(url):
            return "list"
        return "unknown"

    path = urlparse(url).path
    if path.startswith(site.list_path):
        return "list"
    if path.startswith(site.chapter_path):
        return "chapter"
    return "unknown"
