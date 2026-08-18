"""Application launching.

The hard part is not starting a process - it is turning "open VS Code" into a path.
:func:`resolve_app` does that with a four-stage cascade, cheapest and most certain first.
"""

from __future__ import annotations

import difflib
import re

from pydantic import BaseModel, Field

from ..console import get_logger
from ..errors import ToolError
from ..platforms import AppInfo
from ..safety.paths import describe
from ._fsutil import resolve
from .base import RiskLevel, Tool, ToolContext, ToolResult

log = get_logger("tools.apps")

# Names people actually say, mapped to what Windows calls the thing. Users can extend
# this via tools.app_aliases in config.yaml.
BUILTIN_ALIASES: dict[str, str] = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "code": "code",
    "terminal": "wt",
    "windows terminal": "wt",
    "command prompt": "cmd",
    "powershell": "powershell",
    "notepad": "notepad",
    "calculator": "calc",
    "paint": "mspaint",
    "file explorer": "explorer",
    "explorer": "explorer",
    "task manager": "taskmgr",
    "settings": "ms-settings:",
    "control panel": "control",
}


class ListApplicationsArgs(BaseModel):
    filter: str = Field(
        default="",
        description="Only return applications whose name contains this text. "
        "Leave empty to list everything installed.",
    )
    limit: int = Field(default=40, ge=1, le=200, description="Maximum applications to return.")


class ListApplications(Tool[ListApplicationsArgs]):
    name = "list_applications"
    description = """
    List applications installed on this computer. Use this when you are unsure whether
    an app exists or what it is called, before trying to launch it.
    """
    risk = RiskLevel.SAFE
    args_model = ListApplicationsArgs

    def run(self, args: ListApplicationsArgs, ctx: ToolContext) -> ToolResult:
        apps = ctx.platform.list_apps()
        needle = args.filter.strip().lower()
        if needle:
            apps = [a for a in apps if needle in a.name.lower()]
        shown = apps[: args.limit]
        return ToolResult.success(
            f"{len(apps)} application{'s' if len(apps) != 1 else ''} found"
            + (f" matching '{args.filter}'" if needle else "")
            + ".",
            {"applications": [a.name for a in shown], "truncated": len(apps) > len(shown)},
        )


class LaunchApplicationArgs(BaseModel):
    app: str = Field(
        description="Application to launch, as a person would say it: 'Chrome', "
        "'VS Code', 'Notepad', 'Spotify'."
    )
    open_path: str = Field(
        default="",
        description="Optional file or folder to open in that application, e.g. a project "
        "folder for VS Code. Leave empty to just start the app.",
    )
    args: list[str] = Field(
        default_factory=list, description="Extra command-line arguments, rarely needed."
    )


class LaunchApplication(Tool[LaunchApplicationArgs]):
    name = "launch_application"
    description = """
    Start an application, optionally opening a file or folder in it.
    'Open VS Code and load my project' is one call: app='VS Code', open_path='<project>'.
    If the app is already running, prefer control_window with action='focus' instead.
    """
    risk = RiskLevel.CAUTION
    args_model = LaunchApplicationArgs

    def preview(self, args: LaunchApplicationArgs, ctx: ToolContext) -> str:
        target = f" with {args.open_path}" if args.open_path else ""
        return f"Launch {args.app}{target}"

    def run(self, args: LaunchApplicationArgs, ctx: ToolContext) -> ToolResult:
        app = resolve_app(args.app, ctx)
        if app is None:
            suggestions = _suggest(args.app, ctx)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise ToolError(f"No application named '{args.app}' was found.{hint}")

        extra = list(args.args)
        opened = None
        if args.open_path:
            opened = resolve(ctx, args.open_path, must_exist=True)
            extra.append(str(opened))
            ctx.remember_path(str(opened))

        try:
            ctx.platform.launch(app.target, extra)
        except OSError as exc:
            raise ToolError(f"Could not launch {app.name}: {exc}") from exc

        detail = f" with {describe(opened)}" if opened else ""
        return ToolResult.success(
            f"Launched {app.name}{detail}.",
            {"app": app.name, "target": app.target, "opened": str(opened) if opened else None},
        )


def resolve_app(spoken: str, ctx: ToolContext) -> AppInfo | None:
    """Turn a spoken app name into something launchable.

    Order matters - each stage is more of a guess than the one before it:
    user aliases, built-in aliases, exact/substring match on installed apps,
    then fuzzy match as a last resort.
    """
    raw = spoken.strip()
    if not raw:
        return None
    key = _normalise(raw)

    # 1. User config wins outright.
    user_aliases = {_normalise(k): v for k, v in ctx.settings.tools.app_aliases.items()}
    if key in user_aliases:
        return AppInfo(raw, user_aliases[key], "alias")

    # 2. Built-in aliases, resolved against PATH so we get a real executable.
    if key in BUILTIN_ALIASES:
        command = BUILTIN_ALIASES[key]
        if command.endswith(":"):  # a shell URI like ms-settings:
            return AppInfo(raw, command, "alias")
        found = ctx.platform.which(command)
        if found:
            return AppInfo(raw, found, "alias")

    apps = ctx.platform.list_apps()
    by_name = {_normalise(a.name): a for a in apps}

    # 3. Exact, then prefix, then substring - prefer the shortest match, because
    #    "Word" should beat "Word Viewer Add-in" for the query "word".
    if key in by_name:
        return by_name[key]
    prefix = [a for n, a in by_name.items() if n.startswith(key)]
    if prefix:
        return min(prefix, key=lambda a: len(a.name))
    substring = [a for n, a in by_name.items() if key in n]
    if substring:
        return min(substring, key=lambda a: len(a.name))

    # 4. Fuzzy, for transcription slips ("visual studio kode").
    close = difflib.get_close_matches(key, list(by_name), n=1, cutoff=0.8)
    if close:
        return by_name[close[0]]

    # 5. Maybe it is simply a command on PATH we have no shortcut for.
    found = ctx.platform.which(raw)
    return AppInfo(raw, found, "path") if found else None


def _suggest(spoken: str, ctx: ToolContext, n: int = 3) -> list[str]:
    names = [a.name for a in ctx.platform.list_apps()]
    return difflib.get_close_matches(spoken, names, n=n, cutoff=0.5)


def _normalise(text: str) -> str:
    """Lowercase, drop punctuation and version noise, collapse whitespace."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    cleaned = re.sub(r"\b(app|application|program|\d{4})\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


APP_TOOLS = [ListApplications(), LaunchApplication()]
