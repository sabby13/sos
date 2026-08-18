"""The OS-facing interface.

Everything platform-specific lives behind this class. Tools never call ``ctypes``,
``os.startfile``, or ``subprocess`` for OS integration directly - they go through the
adapter, which is why porting OpenJarvis to macOS or Linux means writing one file rather
than auditing the whole tool layer.

The base class implements what is genuinely portable (``webbrowser``, ``shutil``) and
raises :class:`PlatformUnsupported` for the rest, so an unported capability fails with a
clear sentence instead of a mystery traceback.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from ..errors import PlatformUnsupported


@dataclass(slots=True)
class AppInfo:
    """An installed application the agent can launch."""

    name: str
    target: str  # path to an .exe/.lnk/.app, or a bare command on PATH
    source: str = "unknown"  # start-menu | path | alias | applications


@dataclass(slots=True)
class WindowInfo:
    """A top-level window."""

    handle: int
    title: str
    process: str = ""
    minimized: bool = False


class PlatformAdapter:
    """Default implementation: portable bits work, OS-specific bits refuse politely."""

    name = "generic"

    # ------------------------------------------------------------------ apps

    def list_apps(self) -> list[AppInfo]:
        """Every launchable application we can discover."""
        raise PlatformUnsupported(f"Listing applications is not implemented on {sys.platform}.")

    def launch(self, target: str, args: list[str] | None = None) -> int | None:
        """Start ``target``. Returns a PID when we have one."""
        raise PlatformUnsupported(f"Launching applications is not implemented on {sys.platform}.")

    def which(self, command: str) -> str | None:
        return shutil.which(command)

    # --------------------------------------------------------------- windows

    def list_windows(self) -> list[WindowInfo]:
        raise PlatformUnsupported(f"Window listing is not implemented on {sys.platform}.")

    def focus_window(self, handle: int) -> bool:
        raise PlatformUnsupported(f"Window focus is not implemented on {sys.platform}.")

    def close_window(self, handle: int) -> bool:
        raise PlatformUnsupported(f"Closing windows is not implemented on {sys.platform}.")

    def set_window_state(self, handle: int, state: str) -> bool:
        """``state`` is one of minimize | maximize | restore."""
        raise PlatformUnsupported(f"Window state changes are not implemented on {sys.platform}.")

    # ----------------------------------------------------------------- files

    def reveal(self, path: Path) -> bool:
        """Open a folder in the file manager, selecting ``path`` if it is a file."""
        raise PlatformUnsupported(f"Revealing files is not implemented on {sys.platform}.")

    def open_with_default_app(self, path: Path) -> bool:
        """Hand a file to whatever application owns its type."""
        raise PlatformUnsupported(f"Opening files is not implemented on {sys.platform}.")

    def move_to_trash(self, path: Path) -> bool:
        """Recycle bin / Trash. Return False if unavailable so callers can fall back."""
        return False

    # --------------------------------------------------------------- browser

    def open_url(self, url: str, browser: str | None = None) -> bool:
        """Portable everywhere ``webbrowser`` works, which is all three platforms."""
        if browser:
            try:
                controller = webbrowser.get(browser)
            except webbrowser.Error:
                controller = webbrowser.get()  # named browser not registered; use default
            return controller.open(url)
        return webbrowser.open(url)

    # ----------------------------------------------------------------- audio

    def play_audio_file(self, path: Path, *, block: bool = True) -> None:
        """Play a wav/mp3 file. Used by the network-backed TTS engines."""
        player = _find_cli_player()
        if player is None:
            raise PlatformUnsupported(
                f"No audio player available on {sys.platform}.",
                hint="Install ffmpeg (ffplay) or mpv, or set speech.tts_engine: system.",
            )
        cmd = [*player, str(path)]
        if block:
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_audio(self) -> None:
        """Best-effort interrupt of whatever :meth:`play_audio_file` started."""
        return None


def _find_cli_player() -> list[str] | None:
    for cmd, flags in (
        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
        ("mpv", ["--no-video", "--really-quiet"]),
        ("afplay", []),
        ("mpg123", ["-q"]),
        ("aplay", ["-q"]),
    ):
        if shutil.which(cmd):
            return [cmd, *flags]
    return None
