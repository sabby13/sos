"""Path resolution and guard rails.

Every file-touching tool routes through :func:`resolve_path`, so there is exactly one
place that decides what "safe path" means. The rules deliberately err toward refusing:
a blocked action costs the user one sentence of explanation, an unguarded one can cost
them their Windows install.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..errors import SafetyError

# Directories that must never be written to, whatever the config says.
_WINDOWS_PROTECTED = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    r"C:\System Volume Information",
)
_POSIX_PROTECTED = (
    "/bin", "/sbin", "/usr", "/etc", "/boot", "/dev", "/proc", "/sys", "/var/lib",
    "/System", "/Library", "/Applications",
)

# Friendly names the model can use instead of absolute paths.
_WELL_KNOWN = {
    "home": Path.home,
    "desktop": lambda: Path.home() / "Desktop",
    "documents": lambda: Path.home() / "Documents",
    "downloads": lambda: Path.home() / "Downloads",
    "pictures": lambda: Path.home() / "Pictures",
    "music": lambda: Path.home() / "Music",
    "videos": lambda: Path.home() / "Videos",
}


def protected_roots() -> tuple[Path, ...]:
    """Directories nothing may be written *inside*, at any depth."""
    raw = _WINDOWS_PROTECTED if sys.platform == "win32" else _POSIX_PROTECTED
    return tuple(Path(p) for p in raw)


def is_filesystem_root(path: Path) -> bool:
    """True for ``C:\\`` or ``/`` - the only paths that are their own parent."""
    return path == path.parent


def expand(raw: str) -> Path:
    """Expand ``~``, environment variables, and well-known folder names."""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise SafetyError("An empty path was given.")

    key = text.lower().replace("_", " ").replace("my ", "").strip()
    if key in _WELL_KNOWN:
        return _WELL_KNOWN[key]()

    return Path(os.path.expandvars(os.path.expanduser(text)))


def is_within(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or lives underneath it."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def resolve_path(
    raw: str,
    *,
    cwd: str | None = None,
    must_exist: bool = False,
    for_write: bool = False,
    allowed_roots: list[str] | None = None,
    denied_paths: list[str] | None = None,
    protect_system: bool = True,
) -> Path:
    """Turn a model-supplied string into a vetted absolute :class:`Path`.

    Raises :class:`SafetyError` if the path is off-limits, and :class:`FileNotFoundError`
    if ``must_exist`` and it is not there.
    """
    path = expand(raw)
    if not path.is_absolute():
        base = Path(cwd) if cwd else Path.cwd()
        path = base / path

    try:
        # strict=False so we can resolve destinations that do not exist yet.
        path = path.resolve(strict=False)
    except OSError as exc:
        raise SafetyError(f"Could not resolve path {raw!r}: {exc}") from exc

    if must_exist and not path.exists():
        raise FileNotFoundError(f"{path} does not exist.")

    for denied in denied_paths or []:
        if is_within(path, expand(denied)):
            raise SafetyError(f"{path} is inside a denied path ({denied}).")

    if for_write:
        if protect_system:
            # Writing *at* a drive root is virtually always a mistake, but writing
            # somewhere under it obviously is not - so this is an equality check on the
            # path and its parent, never a containment check.
            if is_filesystem_root(path) or is_filesystem_root(path.parent):
                raise SafetyError(
                    f"{path} sits at a drive root, which is a protected system location.",
                    hint="Pick a folder inside your home directory instead.",
                )
            for root in protected_roots():
                if path == root or is_within(path, root):
                    raise SafetyError(
                        f"{path} is inside a protected system location ({root}).",
                        hint="Set safety.protect_system_paths: false only if you truly mean it.",
                    )
        roots = [expand(r) for r in (allowed_roots or [])]
        if roots and not any(is_within(path, r) for r in roots):
            pretty = ", ".join(str(r) for r in roots)
            raise SafetyError(
                f"{path} is outside the allowed roots ({pretty}).",
                hint="Add the folder to safety.allowed_roots in your config to permit it.",
            )

    return path


def describe(path: Path) -> str:
    """Short, readable rendering for prompts - collapses the home directory to ``~``."""
    try:
        return "~" + os.sep + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
