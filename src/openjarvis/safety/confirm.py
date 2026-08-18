"""Asking the user "are you sure?" - by keyboard, by voice, or not at all."""

from __future__ import annotations

import re
from typing import Protocol

from rich.markup import escape
from rich.panel import Panel

from ..console import console

_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|confirm|affirmative)\b", re.I)
_NO = re.compile(r"\b(no|nope|nah|stop|cancel|don'?t|negative|abort)\b", re.I)


class Confirmer(Protocol):
    def confirm(self, title: str, detail: str) -> bool: ...


class AutoConfirmer:
    """Always yes. Used by ``--yes`` and by tests."""

    def confirm(self, title: str, detail: str) -> bool:
        console.print(f"[warn]auto-approved:[/warn] [muted]{escape(detail)}[/muted]")
        return True


class DenyConfirmer:
    """Always no. Used by ``--dry-run`` so you can watch a plan without it executing."""

    def confirm(self, title: str, detail: str) -> bool:
        console.print(f"[muted]dry run, skipping:[/muted] {escape(detail)}")
        return False


class ConsoleConfirmer:
    """Keyboard yes/no with a preview panel."""

    def confirm(self, title: str, detail: str) -> bool:
        console.print(
            Panel(
                escape(detail),
                title=f"[warn]{escape(title)}[/warn]",
                border_style="yellow",
                expand=False,
            )
        )
        try:
            answer = console.input("[warn]allow this? [y/N][/warn] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("[danger]cancelled[/danger]")
            return False
        return answer in {"y", "yes"}


class VoiceConfirmer:
    """Speak the request, listen for a spoken yes/no, fall back to the keyboard.

    ``listen`` returns the next transcribed utterance, or None on timeout.
    """

    def __init__(self, speak, listen, *, attempts: int = 2) -> None:  # type: ignore[no-untyped-def]
        self._speak = speak
        self._listen = listen
        self._attempts = attempts
        self._fallback = ConsoleConfirmer()

    def confirm(self, title: str, detail: str) -> bool:
        console.print(
            Panel(
                escape(detail),
                title=f"[warn]{escape(title)}[/warn]",
                border_style="yellow",
                expand=False,
            )
        )
        question = f"{detail}. Should I go ahead?"
        for attempt in range(self._attempts):
            self._speak(question if attempt == 0 else "Sorry - yes or no?")
            heard = self._listen()
            if heard is None:
                break
            console.print(f"[muted]heard: {escape(heard)}[/muted]")
            # Check "no" first: "no, don't do it" contains no yes-words, but
            # "yes... no wait" should not read as approval either.
            if _NO.search(heard):
                return False
            if _YES.search(heard):
                return True
        self._speak("I didn't catch that, so I'll ask on screen.")
        return self._fallback.confirm(title, detail)
