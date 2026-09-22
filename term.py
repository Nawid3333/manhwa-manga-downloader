"""Cross-platform terminal color helpers and rich prompts.

Provides a plain-terminal fallback if `rich` is missing or if stdout is not a
TTY. This mirrors the style used in the sibling scraper projects.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

# Mirrors every cinfo/cwarning/cerror/csuccess call into a per-run log file
# (see init_file_logging, called once from main.py) so a run is
# reconstructable after the terminal scrollback is gone. Silent (no handler,
# no-op writes) until init_file_logging attaches a file handler -- tests and
# library-style imports of term.py never touch disk.
_file_logger = logging.getLogger("mangadl")
_file_logger.setLevel(logging.DEBUG)
_file_logger.propagate = False

_rich_ok = False
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm
    from rich.text import Text

    _console = Console()
    _rich_ok = _console.is_terminal or True
except Exception:  # pragma: no cover - fallback
    _console = None


def _plain_print(message: str = "") -> None:
    print(message)


def cprint(
    message: str = "",
    *,
    color: str = "white",
    panel: bool = False,
    title: str | None = None,
) -> None:
    """Print a colored message. Falls back to plain text if rich is unavailable."""
    if _rich_ok and _console is not None:
        text = Text(message, style=color)
        if panel:
            _console.print(Panel(text, title=title, border_style=color))
        else:
            _console.print(text)
    else:
        prefix = ""
        if title:
            prefix = f"[{title}] "
        print(prefix + message)


def cinput(prompt: str, *, color: str = "cyan") -> str:
    """Read a line of input with a colored prompt."""
    if _rich_ok and _console is not None:
        _console.print(Text(prompt, style=color), end="")
    else:
        print(prompt, end="")
    try:
        return input()
    except (EOFError, KeyboardInterrupt):
        return ""


def cconfirm(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    if _rich_ok and _console is not None:
        try:
            return Confirm.ask(Text(prompt, style="yellow"), default=default)
        except Exception:
            pass
    suffix = " [Y/n]" if default else " [y/N]"
    answer = cinput(prompt + suffix, color="yellow").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def init_file_logging(logs_dir: Path) -> Path:
    """Attach a per-run log file under logs_dir.

    Every cerror/cwarning/csuccess/cinfo call is mirrored there (at the
    matching level), plus DEBUG-level request diagnostics (log_debug) that
    would be too noisy for the console -- per-image attempt timing and the
    adaptive concurrency limit, which is exactly what's needed to tell
    "the network hiccuped once" apart from "this site throttles hard past
    concurrency N" after the fact. Returns the log file path so the caller
    can tell the user where it is. Safe to call more than once; each call
    just adds another handler (harmless, but callers should normally call it
    once at startup).
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    _file_logger.addHandler(handler)
    return log_path


def log_debug(message: str) -> None:
    """Diagnostic detail written only to the log file, never the console."""
    _file_logger.debug(message)


def cerror(message: str) -> None:
    cprint(message, color="red", panel=True, title="error")
    _file_logger.error(message)


def cwarning(message: str) -> None:
    cprint(message, color="yellow", panel=True, title="warning")
    _file_logger.warning(message)


def csuccess(message: str) -> None:
    cprint(message, color="green", panel=True, title="success")
    _file_logger.info(message)


def cinfo(message: str) -> None:
    cprint(message, color="cyan")
    _file_logger.info(message)


def pause(message: str = "Press Enter to exit...") -> None:
    """Pause for a key press before exiting (helpful on Windows)."""
    if _rich_ok and _console is not None:
        _console.print(Text(message, style="dim"))
    else:
        print(message)
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input()


def clear() -> None:
    """Clear the terminal screen when possible."""
    if _rich_ok and _console is not None:
        _console.clear()
    else:
        print("\033[2J\033[H", end="")


def list_sites_table(sites: Mapping[str, Any]) -> None:
    """Print a table of supported sites."""
    if _rich_ok and _console is not None:
        from rich.table import Table

        table = Table(title="Supported Sites", header_style="bold magenta")
        table.add_column("Key", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Domains", style="white")
        for site in sites.values():
            domains = ", ".join(getattr(site, "domains", ())) if hasattr(site, "domains") else ""
            table.add_row(getattr(site, "key", "?"), getattr(site, "name", "?"), domains)
        _console.print(table)
    else:
        print("Supported sites:")
        for site in sites.values():
            print(f"  - {getattr(site, 'key', '?')}: {getattr(site, 'name', '?')}")


def prompt_choice(prompt: str, choices: list[str]) -> str:
    """Ask the user to pick one of a list of choices."""
    if not choices:
        return ""
    for i, choice in enumerate(choices, 1):
        cprint(f"  {i}) {choice}", color="white")
    while True:
        raw = cinput(f"{prompt} (1-{len(choices)}): ", color="yellow").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        if raw in choices:
            return raw
        cerror("Invalid choice. Please enter a number or exact label.")


def prompt_range(total: int) -> list[int]:
    """Ask for a chapter range such as '1-10', 'all', or a single number."""
    raw = cinput(f"Which chapters? (1-{total}, range like 1-10, or 'all'): ", color="yellow").strip()
    if raw.lower() in ("all", "*", ""):
        return list(range(1, total + 1))
    if "-" in raw:
        try:
            start, end = raw.split("-", 1)
            start_num = int(start.strip())
            end_num = int(end.strip())
            return list(range(max(1, start_num), min(total, end_num) + 1))
        except ValueError:
            pass
    if raw.isdigit():
        num = int(raw)
        if 1 <= num <= total:
            return [num]
    cwarning(f"Could not parse '{raw}' — downloading all chapters instead.")
    return list(range(1, total + 1))
