"""Window management: see what's open, focus it, move it out of the way, close it.

Window operations are folded into a single ``control_window`` tool with an action
parameter rather than four separate tools. Smaller models pick the right tool far more
reliably from a short list, and the actions here share all their arguments anyway.
"""

from __future__ import annotations

import difflib
from typing import Literal

from pydantic import BaseModel, Field

from ..errors import ToolError
from ..platforms import WindowInfo
from .base import RiskLevel, Tool, ToolContext, ToolResult

WindowAction = Literal["focus", "minimize", "maximize", "restore", "close"]


class ListWindowsArgs(BaseModel):
    filter: str = Field(
        default="", description="Only return windows whose title or process contains this text."
    )


class ListWindows(Tool[ListWindowsArgs]):
    name = "list_windows"
    description = """
    List the windows currently open on screen, with their titles.
    Use this to find out what is running before focusing or closing something, and to
    answer questions like 'what do I have open?'.
    """
    risk = RiskLevel.SAFE
    args_model = ListWindowsArgs

    def run(self, args: ListWindowsArgs, ctx: ToolContext) -> ToolResult:
        windows = ctx.platform.list_windows()
        needle = args.filter.strip().lower()
        if needle:
            windows = [
                w for w in windows
                if needle in w.title.lower() or needle in w.process.lower()
            ]
        return ToolResult.success(
            f"{len(windows)} window{'s' if len(windows) != 1 else ''} open.",
            {
                "windows": [
                    {"title": w.title, "process": w.process, "minimized": w.minimized}
                    for w in windows
                ]
            },
        )


class ControlWindowArgs(BaseModel):
    title: str = Field(
        description="Part of the window title or process name, e.g. 'chrome', 'notepad', "
        "'budget.xlsx'. Matched case-insensitively."
    )
    action: WindowAction = Field(
        default="focus",
        description="focus brings it to the front; minimize/maximize/restore change its size; "
        "close asks the application to quit (it may still prompt to save).",
    )


class ControlWindow(Tool[ControlWindowArgs]):
    name = "control_window"
    description = """
    Focus, minimize, maximize, restore, or close an open window, found by its title.
    Use action='focus' for 'switch to Chrome' when Chrome is already running - it is
    faster and less disruptive than launching a second instance.
    """
    risk = RiskLevel.CAUTION
    args_model = ControlWindowArgs

    def preview(self, args: ControlWindowArgs, ctx: ToolContext) -> str:
        return f"{args.action.capitalize()} the window matching '{args.title}'"

    def run(self, args: ControlWindowArgs, ctx: ToolContext) -> ToolResult:
        windows = ctx.platform.list_windows()
        match = _best_window(args.title, windows)
        if match is None:
            open_titles = [w.title for w in windows][:8]
            raise ToolError(
                f"No open window matches '{args.title}'. Currently open: "
                + (", ".join(open_titles) or "nothing")
            )

        if args.action == "focus":
            ok = ctx.platform.focus_window(match.handle)
        elif args.action == "close":
            ok = ctx.platform.close_window(match.handle)
        else:
            ok = ctx.platform.set_window_state(match.handle, args.action)

        if not ok:
            raise ToolError(
                f"The window '{match.title}' would not {args.action}. "
                "It may have closed already, or be running as administrator."
            )

        verbs = {
            "focus": "Brought to the front",
            "minimize": "Minimized",
            "maximize": "Maximized",
            "restore": "Restored",
            "close": "Closed",
        }
        return ToolResult.success(
            f"{verbs[args.action]}: {match.title}.",
            {"title": match.title, "process": match.process, "action": args.action},
        )


def _best_window(needle: str, windows: list[WindowInfo]) -> WindowInfo | None:
    """Pick the window a person meant.

    Exact title, then substring on title, then process name, then fuzzy. Ties break
    toward the shortest title, which is usually the main window rather than a dialog.
    """
    key = needle.strip().lower()
    if not key or not windows:
        return None

    exact = [w for w in windows if w.title.lower() == key]
    if exact:
        return exact[0]
    in_title = [w for w in windows if key in w.title.lower()]
    if in_title:
        return min(in_title, key=lambda w: len(w.title))
    in_process = [w for w in windows if key in w.process.lower().removesuffix(".exe")]
    if in_process:
        return min(in_process, key=lambda w: len(w.title))

    titles = {w.title.lower(): w for w in windows}
    close = difflib.get_close_matches(key, list(titles), n=1, cutoff=0.6)
    return titles[close[0]] if close else None


WINDOW_TOOLS = [ListWindows(), ControlWindow()]
