"""Site registry and shared configuration.

Drivers self-register: any module in src/sites/ that exports a `driver`
(a SiteDriver instance) is discovered automatically — dropping a new file
into src/sites/ is enough to add a site.

A URL is resolved to a driver in two steps (resolve_site): by domain first
(site_for_url, no network), and only when nothing matches is the page
fetched once and offered to every driver's sniff() in descending priority.
Domain matches stay authoritative so a site with a dedicated driver is
never handed to the generic fallback just because it also looks generic.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import dotenv_values, load_dotenv

from src.base import SiteDriver
from src.common import client as _plain_client
from term import log_debug

ROOT_DIR = Path(__file__).resolve().parent


def is_checkout(root: Path) -> bool:
    """True when the app runs from its source tree (a clone, or `pip install -e .`).

    An installed copy (a wheel, `pip install .`) lives in site-packages: a
    folder that is hard to find, vanishes with its venv and is often not
    writable, and nobody keeps a `.env` there.
    """
    if {"site-packages", "dist-packages"} & set(root.parts):
        return False
    return (root / "pyproject.toml").is_file()


# Where downloads, logs and `.env` live: the source tree when running from
# one (the setup the README describes, and the downloads/ path OmniScan's
# docs point at), else the folder `mangadl` was started in.
DATA_DIR = ROOT_DIR if is_checkout(ROOT_DIR) else Path.cwd()
DOWNLOADS_DIR = DATA_DIR / "downloads"
LOGS_DIR = DATA_DIR / "logs"

# The settings a `.env` outside the source tree may set. A folder the app is
# merely started in is not trusted with the rest: a planted HTTPS_PROXY plus
# SSL_CERT_FILE (both honored by httpx) would hand the mangago session to
# whoever wrote that file.
APP_ENV_PREFIXES = ("MANGAGO_", "MANGADEX_")


def load_env(path: Path, *, trusted: bool) -> None:
    """Load `path` into os.environ without overriding real environment variables.

    Untrusted, only this app's own settings (APP_ENV_PREFIXES) are taken.
    """
    if trusted:
        load_dotenv(path)
        return
    for key, value in dotenv_values(path).items():
        if value is not None and key.startswith(APP_ENV_PREFIXES):
            os.environ.setdefault(key, value)


# Must run before driver discovery below: some drivers (e.g. mangago, which
# needs a logged-in session cookie) read their own env vars whenever a
# request is made, so those vars have to already be in os.environ by then.
load_env(DATA_DIR / ".env", trusted=DATA_DIR == ROOT_DIR)

# ---- conversion settings ---------------------------------------------------
# Target format is always JPEG; non-JPEG downloads are converted after a run
# on the CPU (all cores). GPU conversion was evaluated and rejected: the
# workload is codec-bound (decode + libjpeg encode), no GPU JPEG encoder
# exists in any available runtime, and measured CPU throughput (~300 img/s
# on all cores) already outruns network download. See AGENTS.md.
CONVERT_TO_JPEG = True  # master switch
JPEG_QUALITY = 90


@dataclass(frozen=True)
class Site:
    """Metadata about a registered site (built from its driver)."""

    key: str
    name: str
    domains: tuple[str, ...]
    module: str
    driver: SiteDriver = field(compare=False)


def _registry_warning(message: str) -> None:
    """Report a registry problem on stderr.

    Discovery runs at import time, before main() can route the console for
    `--json`, so anything printed to stdout here would land in front of the
    JSON object a calling script parses.
    """
    print(f"[registry] {message}", file=sys.stderr)


def _discover_drivers() -> dict[str, Site]:
    """Import every src/sites/*.py module and collect exported `driver`s."""
    import src.sites as sites_pkg

    drivers: dict[str, Site] = {}
    for info in pkgutil.iter_modules(sites_pkg.__path__):
        if info.name.startswith("_"):
            continue
        module_name = f"{sites_pkg.__name__}.{info.name}"
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:  # a broken driver must not kill the app
            _registry_warning(f"failed to load site module {module_name}: {exc}")
            continue
        driver = getattr(mod, "driver", None)
        if not isinstance(driver, SiteDriver) or not driver.key:
            continue
        if driver.key in drivers:
            _registry_warning(f"duplicate site key {driver.key!r} in {module_name}")
            continue
        drivers[driver.key] = Site(
            key=driver.key,
            name=driver.name or driver.key,
            domains=tuple(driver.domains),
            module=module_name,
            driver=driver,
        )
    return drivers


SUPPORTED_SITES: dict[str, Site] = _discover_drivers()


def all_drivers() -> list[SiteDriver]:
    return [site.driver for site in SUPPORTED_SITES.values()]


def site_for_url(url: str) -> Site | None:
    """Return the registered site for a URL, or None."""
    for site in SUPPORTED_SITES.values():
        if site.driver.matches(url):
            return site
    return None


async def fetch_page_html(url: str) -> str:
    """Fetch a page once with the plain browser-like client (redirects followed)."""
    async with _plain_client("", follow_redirects=True) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return resp.text


def sniff_site(url: str, html: str) -> Site | None:
    """Offer fetched page content to every driver's sniff(), highest priority first."""
    ranked = sorted(SUPPORTED_SITES.values(), key=lambda s: s.driver.priority, reverse=True)
    for site in ranked:
        if site.driver.sniff(url, html):
            return site
    return None


async def resolve_site(url: str, *, fetch_html: Callable[[str], Awaitable[str]] | None = None) -> Site | None:
    """Return the site for a URL: domain match first, else by sniffing the fetched page.

    `fetch_html` exists so callers (and tests) can substitute the page fetch;
    the default goes to the network once. A page that cannot be fetched at
    all resolves to None rather than to the generic fallback -- a download
    from it could not succeed either.
    """
    site = site_for_url(url)
    if site is not None:
        return site
    fetch = fetch_html or fetch_page_html
    try:
        html = await fetch(url)
    except httpx.HTTPError as exc:
        log_debug(f"resolve_site: could not fetch {url} for sniffing: {exc}")
        return None
    return sniff_site(url, html)


def classify_url(url: str, site: Site) -> str:
    """Return 'list', 'chapter', or 'unknown' via the site's driver."""
    return site.driver.classify(url)
