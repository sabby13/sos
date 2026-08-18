"""Platform adapter selection."""

from __future__ import annotations

import functools
import sys

from .base import AppInfo, PlatformAdapter, WindowInfo

__all__ = ["AppInfo", "PlatformAdapter", "WindowInfo", "get_adapter"]


@functools.lru_cache(maxsize=1)
def get_adapter() -> PlatformAdapter:
    """Return the adapter for the running OS (one shared instance)."""
    if sys.platform == "win32":
        from .windows import WindowsAdapter

        return WindowsAdapter()
    if sys.platform == "darwin":
        from .posix import MacOSAdapter

        return MacOSAdapter()
    if sys.platform.startswith("linux"):
        from .posix import LinuxAdapter

        return LinuxAdapter()
    return PlatformAdapter()
