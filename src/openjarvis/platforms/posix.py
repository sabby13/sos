"""macOS and Linux adapters.

These are deliberately thinner than the Windows one - OpenJarvis is Windows-first for
now. They implement what can be done with the standard OS command-line tools, and inherit
:class:`PlatformUnsupported` from the base class for the rest. Contributions welcome; the
interface you need to satisfy is entirely in ``platforms/base.py``.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
from pathlib import Path

from ..console import get_logger
from ..errors import PlatformUnsupported
from .base import AppInfo, PlatformAdapter, WindowInfo

log = get_logger("platform.posix")


class MacOSAdapter(PlatformAdapter):
    name = "macos"

    def list_apps(self) -> list[AppInfo]:
        apps: dict[str, AppInfo] = {}
        for root in (Path("/Applications"), Path.home() / "Applications",
                     Path("/System/Applications")):
            if not root.is_dir():
                continue
            for entry in root.glob("**/*.app"):
                apps.setdefault(entry.stem.lower(), AppInfo(entry.stem, str(entry), "applications"))
        return sorted(apps.values(), key=lambda a: a.name.lower())

    def launch(self, target: str, args: list[str] | None = None) -> int | None:
        cmd = ["open", "-a", target] if target.endswith(".app") or "/" not in target else [target]
        if args:
            cmd += ["--args", *args] if cmd[0] == "open" else args
        subprocess.Popen(cmd)
        return None

    def list_windows(self) -> list[WindowInfo]:
        script = (
            'tell application "System Events" to get {name, name of windows} of '
            "(every process whose visible is true)"
        )
        try:
            out = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PlatformUnsupported(f"Could not query windows via osascript: {exc}") from exc
        # AppleScript returns a flat comma-separated list; good enough to name windows,
        # but the handles are synthetic, so focus works by app name instead.
        titles = [t.strip() for t in out.stdout.split(",") if t.strip()]
        return [WindowInfo(handle=i, title=t, process=t) for i, t in enumerate(titles)]

    def focus_window(self, handle: int) -> bool:
        raise PlatformUnsupported(
            "Focusing a specific window on macOS needs Accessibility permissions.",
            hint="Use the app launcher instead - `open -a` raises the app.",
        )

    def reveal(self, path: Path) -> bool:
        subprocess.Popen(["open", "-R", str(path)])
        return True

    def open_with_default_app(self, path: Path) -> bool:
        subprocess.Popen(["open", str(path)])
        return True

    def move_to_trash(self, path: Path) -> bool:
        script = f'tell application "Finder" to delete POSIX file "{path}"'
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, check=False, timeout=15
        )
        return result.returncode == 0

    def play_audio_file(self, path: Path, *, block: bool = True) -> None:
        cmd = ["afplay", str(path)]
        if block:
            subprocess.run(cmd, check=False)
        else:
            subprocess.Popen(cmd)


class LinuxAdapter(PlatformAdapter):
    name = "linux"

    def list_apps(self) -> list[AppInfo]:
        apps: dict[str, AppInfo] = {}
        roots = [
            Path("/usr/share/applications"),
            Path("/usr/local/share/applications"),
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications",
        ]
        for root in roots:
            if not root.is_dir():
                continue
            for entry in root.glob("*.desktop"):
                parsed = _parse_desktop_entry(entry)
                if parsed:
                    apps.setdefault(parsed.name.lower(), parsed)
        return sorted(apps.values(), key=lambda a: a.name.lower())

    def launch(self, target: str, args: list[str] | None = None) -> int | None:
        if target.endswith(".desktop") and shutil.which("gio"):
            subprocess.Popen(["gio", "launch", target, *(args or [])])
            return None
        proc = subprocess.Popen([target, *(args or [])], start_new_session=True)
        return proc.pid

    def list_windows(self) -> list[WindowInfo]:
        if not shutil.which("wmctrl"):
            raise PlatformUnsupported(
                "Window control on Linux needs wmctrl.", hint="Install it: apt install wmctrl"
            )
        out = subprocess.run(
            ["wmctrl", "-lp"], capture_output=True, text=True, check=False, timeout=10
        )
        windows: list[WindowInfo] = []
        for line in out.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            windows.append(WindowInfo(handle=int(parts[0], 16), title=parts[4], process=parts[2]))
        return windows

    def focus_window(self, handle: int) -> bool:
        if not shutil.which("wmctrl"):
            return False
        result = subprocess.run(
            ["wmctrl", "-i", "-a", hex(handle)], check=False, capture_output=True, timeout=10
        )
        return result.returncode == 0

    def close_window(self, handle: int) -> bool:
        if not shutil.which("wmctrl"):
            return False
        result = subprocess.run(
            ["wmctrl", "-i", "-c", hex(handle)], check=False, capture_output=True, timeout=10
        )
        return result.returncode == 0

    def reveal(self, path: Path) -> bool:
        target = path if path.is_dir() else path.parent
        subprocess.Popen(["xdg-open", str(target)])
        return True

    def open_with_default_app(self, path: Path) -> bool:
        subprocess.Popen(["xdg-open", str(path)])
        return True

    def move_to_trash(self, path: Path) -> bool:
        if not shutil.which("gio"):
            return False
        result = subprocess.run(
            ["gio", "trash", str(path)], check=False, capture_output=True, timeout=15
        )
        return result.returncode == 0


def _parse_desktop_entry(path: Path) -> AppInfo | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
        section = parser["Desktop Entry"]
    except (OSError, KeyError, configparser.Error) as exc:
        log.debug("skipping %s: %s", path, exc)
        return None
    if section.get("NoDisplay", "false").lower() == "true":
        return None
    name = section.get("Name")
    if not name:
        return None
    return AppInfo(name=name, target=str(path), source="applications")
