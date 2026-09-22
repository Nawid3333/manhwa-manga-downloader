"""Site registry and shared configuration.

Drivers self-register: any module in src/sites/ that exports a `driver`
(a SiteDriver instance) is discovered automatically — dropping a new file
into src/sites/ is enough to add a site.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from src.base import SiteDriver

ROOT_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = ROOT_DIR / "downloads"
LOGS_DIR = ROOT_DIR / "logs"

# Must run before driver discovery below: some drivers (e.g. mangago, which
# needs a logged-in session cookie) read their own env vars whenever a
# request is made, so those vars have to already be in os.environ by then.
load_dotenv(ROOT_DIR / ".env")

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
            print(f"[registry] failed to load site module {module_name}: {exc}")
            continue
        driver = getattr(mod, "driver", None)
        if not isinstance(driver, SiteDriver) or not driver.key:
            continue
        if driver.key in drivers:
            print(f"[registry] duplicate site key {driver.key!r} in {module_name}")
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


def classify_url(url: str, site: Site) -> str:
    """Return 'list', 'chapter', or 'unknown' via the site's driver."""
    return site.driver.classify(url)
