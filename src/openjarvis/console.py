"""Shared Rich console + logging setup.

Everything user-facing goes through this module so voice mode, text mode, and the
tool layer all render consistently (and so tests can swap in a quiet console).
"""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.theme import Theme

JARVIS_THEME = Theme(
    {
        "jarvis": "bold cyan",
        "user": "bold white",
        "tool": "magenta",
        "ok": "bold green",
        "warn": "bold yellow",
        "danger": "bold red",
        "muted": "dim",
    }
)

console = Console(theme=JARVIS_THEME, highlight=False)

_LOG_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Install a Rich log handler once. Safe to call repeatedly."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        logging.getLogger("openjarvis").setLevel(level.upper())
        return

    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    # These are chatty and rarely what you're debugging.
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"openjarvis.{name}")


# Anything interpolated into a console string must be escaped first. Model output, file
# names, and pip hints all contain square brackets, and rich would silently swallow them
# as style tags - `pip install -e ".[voice]"` renders as `pip install -e "."` otherwise.
def say_user(text: str) -> None:
    console.print(f"[user]you[/user] {escape(text)}")


def say_jarvis(text: str) -> None:
    console.print(f"[jarvis]Sienna[/jarvis] {escape(text)}")


def say_tool(text: str) -> None:
    console.print(f"  [tool]->[/tool] [muted]{escape(text)}[/muted]")


def say_error(text: str, hint: str | None = None) -> None:
    console.print(f"[danger]{escape(text)}[/danger]")
    if hint:
        console.print(f"[muted]{escape(hint)}[/muted]")
