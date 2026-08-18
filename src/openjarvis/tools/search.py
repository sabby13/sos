"""File search - the "find my latest PDF" tool.

Two strategies, tried in order:

1. **Everything** (``es.exe``) if the user has it installed. Instant, whole-machine,
   because it reads the NTFS index. https://voidtools.com
2. A **breadth-first walk** with pruning and a hard deadline. Breadth-first matters:
   the files a person is thinking of are usually a few levels down, so shallow results
   arrive before the walk gets lost in ``node_modules``.

Neither strategy is allowed to run forever - both are bounded by ``search_timeout_s``,
and a partial result is reported honestly as partial rather than as "not found".
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from ..console import get_logger
from ..errors import ToolError
from ..safety.paths import describe, expand, human_size
from .base import RiskLevel, Tool, ToolContext, ToolResult

log = get_logger("tools.search")


@dataclass(slots=True)
class Hit:
    path: Path
    modified: float
    size: int
    is_dir: bool


class FindFilesArgs(BaseModel):
    query: str = Field(
        default="",
        description="Text to match in the file name. Supports wildcards like '*.pdf' or "
        "'report*'. Leave empty to match everything and rely on the other filters.",
    )
    extensions: list[str] = Field(
        default_factory=list,
        description="File extensions to keep, without the dot, e.g. ['pdf'] or ['py','pyi']. "
        "Use this for 'find my Python files' rather than putting '*.py' in query.",
    )
    folder: str = Field(
        default="",
        description="Restrict the search to this folder. Accepts shortcuts like 'downloads' "
        "or 'desktop'. Leave empty to search the user's whole home directory.",
    )
    newest_first: bool = Field(
        default=True,
        description="Sort by modification time, newest first. This is what 'my latest X' means.",
    )
    max_age_days: int = Field(
        default=0, ge=0, description="Only include items modified within this many days. 0 = any."
    )
    include_folders: bool = Field(default=False, description="Also match folders, not just files.")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum results to return.")


class FindFiles(Tool[FindFilesArgs]):
    name = "find_files"
    description = """
    Search the user's computer for files by name, extension, or recency.
    This is the tool for 'find my latest PDF', 'where is my resume', 'search for Python
    files'. It searches the home directory by default - pass folder to narrow it down,
    which is much faster. Returns full paths you can pass to other tools.
    """
    risk = RiskLevel.SAFE
    args_model = FindFilesArgs

    def run(self, args: FindFilesArgs, ctx: ToolContext) -> ToolResult:
        cfg = ctx.settings.tools
        if not args.query and not args.extensions and not args.max_age_days:
            raise ToolError(
                "That search has no filters and would match every file on the machine. "
                "Ask the user for a name, an extension, or a folder."
            )

        roots = _resolve_roots(args.folder, ctx)
        deadline = time.monotonic() + cfg.search_timeout_s
        extensions = {e.lower().lstrip(".") for e in args.extensions if e.strip()}
        cutoff = time.time() - args.max_age_days * 86400 if args.max_age_days else 0.0

        hits, complete = _search_everything(args, roots, extensions, cutoff, deadline)
        engine = "everything"
        if hits is None:
            engine = "walk"
            hits, complete = _search_walk(args, roots, extensions, cutoff, deadline, cfg)

        hits.sort(key=lambda h: h.modified if args.newest_first else 0, reverse=args.newest_first)
        if not args.newest_first:
            hits.sort(key=lambda h: h.path.name.lower())
        hits = hits[: args.limit]

        results = [
            {
                "name": h.path.name,
                "path": str(h.path),
                "type": "folder" if h.is_dir else "file",
                "size": None if h.is_dir else human_size(h.size),
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(h.modified)),
            }
            for h in hits
        ]
        for hit in hits[:3]:
            ctx.remember_path(str(hit.path))

        where = describe(roots[0]) if len(roots) == 1 else f"{len(roots)} folders"
        if not results:
            note = "" if complete else " (search timed out, so this may be incomplete)"
            return ToolResult.success(
                f"Nothing matching that was found in {where}{note}.",
                {"results": [], "complete": complete, "searched": [str(r) for r in roots]},
            )

        headline = results[0]["name"]
        more = f" (showing {len(results)})" if len(results) == args.limit else ""
        return ToolResult.success(
            f"Found {len(results)} match{'es' if len(results) != 1 else ''} in {where}{more}. "
            f"The most recent is {headline}.",
            {"results": results, "complete": complete, "engine": engine},
        )


def _resolve_roots(folder: str, ctx: ToolContext) -> list[Path]:
    if folder.strip():
        root = expand(folder)
        if not root.is_dir():
            raise ToolError(f"{describe(root)} is not a folder that exists.")
        return [root]

    roots = [Path.home()]
    for extra in ctx.settings.tools.search_roots:
        path = expand(extra)
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _matches(name: str, args: FindFilesArgs, extensions: set[str]) -> bool:
    lowered = name.lower()
    if extensions and lowered.rsplit(".", 1)[-1] not in extensions:
        return False
    query = args.query.strip().lower()
    if not query:
        return True
    if any(ch in query for ch in "*?["):
        return fnmatch.fnmatch(lowered, query)
    return query in lowered


def _search_walk(
    args: FindFilesArgs,
    roots: list[Path],
    extensions: set[str],
    cutoff: float,
    deadline: float,
    cfg,  # ToolsConfig
) -> tuple[list[Hit], bool]:
    """Breadth-first scan with pruning. Returns (hits, ran_to_completion)."""
    excluded = {d.lower() for d in cfg.search_exclude_dirs}
    queue: deque[Path] = deque(roots)
    hits: list[Hit] = []
    # Cap collected hits well above `limit` so sorting still has something to choose from.
    hit_cap = max(args.limit * 20, 400)

    while queue:
        if time.monotonic() > deadline or len(hits) >= hit_cap:
            return hits, False
        folder = queue.popleft()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue

                    if is_dir:
                        if entry.name.lower() in excluded or entry.name.startswith("."):
                            continue
                        queue.append(Path(entry.path))
                        if not args.include_folders:
                            continue

                    if not _matches(entry.name, args, extensions if not is_dir else set()):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if cutoff and stat.st_mtime < cutoff:
                        continue
                    hits.append(Hit(Path(entry.path), stat.st_mtime, stat.st_size, is_dir))
        except (PermissionError, OSError) as exc:
            log.debug("skipping %s: %s", folder, exc)
            continue

    return hits, True


def _search_everything(
    args: FindFilesArgs,
    roots: list[Path],
    extensions: set[str],
    cutoff: float,
    deadline: float,
) -> tuple[list[Hit] | None, bool]:
    """Query voidtools Everything via ``es.exe``. Returns (None, True) if unavailable."""
    es = shutil.which("es")
    if es is None:
        return None, True

    terms: list[str] = []
    if args.query.strip():
        terms.append(args.query.strip())
    if extensions:
        terms.append("ext:" + ";".join(sorted(extensions)))
    if not args.include_folders:
        terms.append("file:")
    # Everything scopes by path prefix; one root per query keeps the syntax simple.
    terms.append(f'path:"{roots[0]}"')

    # No column flags: plain one-path-per-line output is what the parser below expects,
    # and we stat() each hit ourselves for size and mtime anyway.
    cmd = [es, "-n", str(max(args.limit * 5, 100)), *terms]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(deadline - time.monotonic(), 1.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Everything query failed, falling back to walk: %s", exc)
        return None, True
    if proc.returncode != 0:
        log.debug("es.exe exited %s, falling back to walk", proc.returncode)
        return None, True

    hits: list[Hit] = []
    for line in proc.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        path = Path(raw)
        try:
            stat = path.stat()
        except OSError:
            continue
        if cutoff and stat.st_mtime < cutoff:
            continue
        hits.append(Hit(path, stat.st_mtime, stat.st_size, path.is_dir()))
    return hits, True


SEARCH_TOOLS = [FindFiles()]
