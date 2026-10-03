"""Cross-platform terminal color helpers and rich prompts.

Provides a plain-terminal fallback if `rich` is missing or if stdout is not a
TTY. This mirrors the style used in the sibling scraper projects.

Everything goes to one switchable stream (set_console_stream): main.py's
`--json` mode points it at stderr so stdout carries nothing but the final
JSON object a calling script will parse.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import IO, Any

# Mirrors every cinfo/cwarning/cerror/csuccess call into a per-run log file
# (see init_file_logging, called once from main.py) so a run is
# reconstructable after the terminal scrollback is gone. Silent (no handler,
# no-op writes) until init_file_logging attaches a file handler -- tests and
# library-style imports of term.py never touch disk.
_file_logger = logging.getLogger("mangadl")
_file_logger.setLevel(logging.DEBUG)
_file_logger.propagate = False

_stream: IO[str] = sys.stdout

_rich_ok = False
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm
    from rich.text import Text

    _console = Console()
    _rich_ok = True
except Exception:  # pragma: no cover - fallback
    _console = None


def set_console_stream(stream: IO[str]) -> None:
    """Send every console message (rich or plain) to `stream` from now on."""
    global _console, _stream
    _stream = stream
    if _rich_ok:
        _console = Console(file=stream)


def _plain_print(message: str = "", *, end: str = "\n") -> None:
    print(message, end=end, file=_stream)


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
        _plain_print(prefix + message)


def cinput(prompt: str, *, color: str = "cyan") -> str:
    """Read a line of input with a colored prompt; end of input reads as an empty answer.

    Ctrl+C is not an answer: it propagates so the run aborts. Read as "",
    it took each prompt's default instead -- at the chapter prompt that is
    every chapter, one Enter away from starting the whole series.
    """
    if _rich_ok and _console is not None:
        _console.print(Text(prompt, style=color), end="")
    else:
        _plain_print(prompt, end="")
    try:
        return input()
    except EOFError:
        return ""


def cconfirm(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    if _rich_ok and _console is not None:
        try:
            return Confirm.ask(Text(prompt, style="yellow"), default=default, console=_console)
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
        _plain_print(message)
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input()


def clear() -> None:
    """Clear the terminal screen when possible."""
    if _rich_ok and _console is not None:
        _console.clear()
    else:
        _plain_print("\033[2J\033[H", end="")


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
        _plain_print("Supported sites:")
        for site in sites.values():
            _plain_print(f"  - {getattr(site, 'key', '?')}: {getattr(site, 'name', '?')}")


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


_CHAPTER_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _whole_chapter(text: str) -> int:
    """The whole chapter number `text` names: '12' and '12.5' are both 12."""
    text = text.strip()
    if not _CHAPTER_NUMBER_RE.fullmatch(text):
        raise ValueError(text)
    return int(text.split(".", 1)[0])


def parse_range(text: str, last: int, first: int = 1) -> list[int]:
    """Parse a chapter selection into sorted chapter numbers within first..last.

    Accepts 'all' (also '*' or blank), a single number ('5'), a range
    ('1-10'), and comma lists mixing both ('1,3,5-7'). Ranges are clamped to
    first..last and single numbers outside it are dropped, so the result can
    be empty; text that is not a selection at all raises ValueError. The
    bounds are chapter *numbers* (what SiteDriver.select_chapters filters
    on), not positions in the listing: a series can start at chapter 0 or
    have gaps, so they come from the listing's lowest and highest number.
    A decimal names the whole chapter it belongs to ('12.5' is 12), since
    select_chapters matches on the whole number and so takes 12.5 with 12.
    """
    raw = text.strip().lower()
    if raw in ("all", "*", ""):
        return list(range(first, last + 1))
    selected: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = _whole_chapter(start_text), _whole_chapter(end_text)
                if start > end:
                    raise ValueError(part)
                selected.update(range(max(first, start), min(last, end) + 1))
            else:
                num = _whole_chapter(part)
                if first <= num <= last:
                    selected.add(num)
        except ValueError:
            raise ValueError(f"Could not parse chapter selection {text!r}") from None
    return sorted(selected)


def note_whole_chapters(text: str) -> None:
    """Say what a decimal in a chapter selection selects (see parse_range)."""
    if "." in text:
        cinfo("Selections are whole chapters: 12.5 selects chapter 12, with 12.5 and any other 12.x.")


def prompt_range(last: int, first: int = 1) -> list[int]:
    """Ask for a chapter range such as '1-10', '1,3,5-7', 'all', or a single number.

    An answer that selects nothing, or is not a selection at all, is asked
    again. Taking it as 'all' put a whole series one Enter away from a typo.
    End of input still reads as "" and so as 'all', which ends the loop even
    when stdin is not a terminal.
    """
    while True:
        raw = cinput(
            f"Which chapters? ({first}-{last}, range like 1-10, list like 1,3,5-7, or 'all'): ", color="yellow"
        )
        try:
            selected = parse_range(raw, last, first)
        except ValueError:
            cwarning(f"Could not read '{raw.strip()}' as a chapter selection. Try again.")
            continue
        if selected:
            note_whole_chapters(raw)
            return selected
        cwarning(f"No chapter in {first}-{last} matches '{raw.strip()}'. Try again.")
