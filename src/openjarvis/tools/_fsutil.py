"""Shared helpers for the file-touching tools."""

from __future__ import annotations

from pathlib import Path

from ..safety.paths import human_size, resolve_path
from .base import ToolContext


def resolve(
    ctx: ToolContext,
    raw: str,
    *,
    must_exist: bool = False,
    for_write: bool = False,
) -> Path:
    """Resolve a model-supplied path against the current safety config."""
    safety = ctx.settings.safety
    return resolve_path(
        raw,
        cwd=ctx.cwd,
        must_exist=must_exist,
        for_write=for_write,
        allowed_roots=safety.allowed_roots,
        denied_paths=safety.denied_paths,
        protect_system=safety.protect_system_paths,
    )


def entry_summary(path: Path) -> dict[str, object]:
    """A compact record of one file or folder, sized for the model's context."""
    try:
        stat = path.stat()
    except OSError:
        return {"name": path.name, "path": str(path), "type": "unknown"}
    is_dir = path.is_dir()
    return {
        "name": path.name,
        "path": str(path),
        "type": "folder" if is_dir else "file",
        "size": None if is_dir else human_size(stat.st_size),
        "modified": _iso(stat.st_mtime),
    }


def _iso(timestamp: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
