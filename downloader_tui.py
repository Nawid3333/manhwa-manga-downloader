"""Terminal UI for the manhwa/manga downloader.

Supports a registry of sites so new scrapers can be added later.
Usage:
  python downloader_tui.py
"""

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from download_wfwf_async import download_chapter_standalone, download_series

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import IntPrompt, Prompt
    from rich.table import Table

    RICH_AVAILABLE = True
except Exception:  # pragma: no cover
    RICH_AVAILABLE = False


console = Console() if RICH_AVAILABLE else None


class Site:
    def __init__(self, name: str, domains: list[str], list_path: str, chapter_path: str):
        self.name = name
        self.domains = domains
        self.list_path = list_path  # e.g. "/list"
        self.chapter_path = chapter_path  # e.g. "/view"


# Registry of supported sites. Add new scrapers here as the project grows.
SUPPORTED_SITES: list[Site] = [
    Site(
        name="wfwf504.com (늑대닷컴)",
        domains=["wfwf504.com"],
        list_path="/list",
        chapter_path="/view",
    ),
]


def print_msg(text: str, style: str = "") -> None:
    """Print a message, using rich if available."""
    if console:
        console.print(text, style=style)
    else:
        print(text)


def print_panel(text: str, title: str = "") -> None:
    """Print a boxed panel, using rich if available."""
    if console:
        console.print(Panel(text, title=title))
    else:
        if title:
            print(f"\n=== {title} ===")
        print(text)


def supported_site_for_url(url: str) -> Site | None:
    """Return the matching Site for a URL, or None if unsupported."""
    parsed = urlparse(url)
    for site in SUPPORTED_SITES:
        if any(d in parsed.netloc for d in site.domains):
            return site
    return None


def classify_url(url: str) -> str:
    """Return 'list', 'chapter', or 'unknown' based on the URL path."""
    site = supported_site_for_url(url)
    if not site:
        return "unknown"
    parsed = urlparse(url)
    if parsed.path.startswith(site.list_path):
        return "list"
    if parsed.path.startswith(site.chapter_path):
        return "chapter"
    return "unknown"


def show_supported_sites() -> None:
    """Display the current supported-sites list."""
    if console:
        table = Table(title="Supported Sites")
        table.add_column("#", justify="right")
        table.add_column("Name")
        table.add_column("Domains")
        for i, site in enumerate(SUPPORTED_SITES, start=1):
            table.add_row(str(i), site.name, ", ".join(site.domains))
        console.print(table)
    else:
        print("Supported sites:")
        for i, site in enumerate(SUPPORTED_SITES, start=1):
            print(f"  {i}. {site.name} ({', '.join(site.domains)})")


def ask(prompt: str, default: str = "") -> str:
    """Ask the user for text input."""
    if console:
        return Prompt.ask(prompt, default=default)
    user = input(f"{prompt}: ")
    return user if user else default


def ask_int(prompt: str, default: int | None = None) -> int | None:
    """Ask the user for an integer."""
    if console:
        return IntPrompt.ask(prompt, default=default)
    user = input(f"{prompt}: ")
    if not user:
        return default
    try:
        return int(user)
    except ValueError:
        return None


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = Prompt.ask(prompt + suffix, default="y" if default else "n") if console else input(prompt + suffix)
    return answer.strip().lower() in ("y", "yes", "")


def parse_range_input(text: str) -> tuple[int | None, int | None] | None:
    """Convert user range text like '1-203' or '203' into (start, end)."""
    text = text.strip()
    if not text:
        return None
    if "-" in text:
        a, b = text.split("-", 1)
        start = int(a) if a.strip() else None
        end = int(b) if b.strip() else None
        return start, end
    try:
        n = int(text)
        return n, n
    except ValueError:
        return None


def sanitize_dir_name(text: str) -> str:
    """Make a string safe for a folder name."""
    return re.sub(r'[\\/:*?"<>|]', "_", text).strip() or "download"


async def run_download(url: str, out_dir: Path, start: int | None, end: int | None) -> None:
    """Dispatch to the correct downloader based on URL type."""
    kind = classify_url(url)
    if kind == "list":
        if start is not None or end is not None:
            await download_series(url, out_dir, start=start, end=end)
        else:
            await download_series(url, out_dir)
    elif kind == "chapter":
        await download_chapter_standalone(url, out_dir)
    else:
        print_msg("Could not classify URL as a list or chapter page.", style="bold red")


def main() -> None:
    print_panel(
        "Async Manhwa / Manga Downloader\nPaste a chapter or series list URL to begin.",
        title="Downloader TUI",
    )
    show_supported_sites()

    url = ask("URL")
    url = url.strip()

    site = supported_site_for_url(url)
    if not site:
        print_msg(f"Unsupported URL: {url}", style="bold red")
        print_msg("Only the following domains are supported:")
        for s in SUPPORTED_SITES:
            print_msg(f"  - {', '.join(s.domains)}")
        sys.exit(1)

    kind = classify_url(url)
    if kind == "unknown":
        print_msg(
            f"URL is from {site.name} but does not look like a list or chapter page.",
            style="bold red",
        )
        sys.exit(1)

    default_dir = sanitize_dir_name(site.name)
    out_dir_name = ask("Output folder", default=default_dir)
    out_dir = Path(out_dir_name.strip())
    out_dir.mkdir(parents=True, exist_ok=True)

    start = end = None
    if kind == "list":
        print_msg("This is a series list URL.", style="bold green")
        mode = ask("Download mode (all / range)", default="all").strip().lower()
        if mode == "range":
            while True:
                rng = ask("Chapter range (e.g. 1-203 or 203)")
                parsed = parse_range_input(rng)
                if parsed:
                    start, end = parsed
                    break
                print_msg("Invalid range. Use e.g. 1-203 or 203.", style="yellow")

    try:
        asyncio.run(run_download(url, out_dir, start, end))
    except KeyboardInterrupt:
        print_msg("\nCancelled by user.", style="yellow")
        sys.exit(130)
    except Exception as e:
        print_msg(f"Error: {e}", style="bold red")
        sys.exit(1)

    print_msg(f"Done. Files saved to: {out_dir.resolve()}", style="bold green")


if __name__ == "__main__":
    main()
