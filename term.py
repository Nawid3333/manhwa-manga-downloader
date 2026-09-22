"""Cross-platform terminal color helpers and rich prompts.

Provides a plain-terminal fallback if `rich` is missing or if stdout is not a
TTY. This mirrors the style used in the sibling scraper projects.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

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


def cerror(message: str) -> None:
    cprint(message, color="red", panel=True, title="error")


def cwarning(message: str) -> None:
    cprint(message, color="yellow", panel=True, title="warning")


def csuccess(message: str) -> None:
    cprint(message, color="green", panel=True, title="success")


def cinfo(message: str) -> None:
    cprint(message, color="cyan")


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
