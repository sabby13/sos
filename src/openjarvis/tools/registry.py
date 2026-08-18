"""The tool router: name -> tool, plus the schema list handed to the model."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..console import get_logger
from ..errors import ToolError
from .base import RiskLevel, Tool

log = get_logger("tools")


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name {tool.name!r}.")
        if not tool.available:
            log.debug("skipping %s: unavailable on this platform", tool.name)
            return
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(
                f"There is no tool called {name!r}. Available tools: "
                + ", ".join(sorted(self._tools))
            ) from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in self._tools.values()]

    def by_risk(self, level: RiskLevel) -> list[Tool]:
        return [t for t in self._tools.values() if t.risk is level]

    def summary(self) -> list[tuple[str, str, str]]:
        """(name, risk, first line of description) - for ``jarvis tools``."""
        rows = []
        for tool in sorted(self._tools.values(), key=lambda t: (t.risk.rank, t.name)):
            first_line = " ".join(tool.description.split())
            rows.append((tool.name, tool.risk.value, first_line))
        return rows


def default_registry() -> ToolRegistry:
    """Every tool shipped with OpenJarvis."""
    from .apps import APP_TOOLS
    from .browser import BROWSER_TOOLS
    from .filesystem import FILESYSTEM_TOOLS
    from .search import SEARCH_TOOLS
    from .windows_ctl import WINDOW_TOOLS

    return ToolRegistry(
        [*FILESYSTEM_TOOLS, *SEARCH_TOOLS, *APP_TOOLS, *WINDOW_TOOLS, *BROWSER_TOOLS]
    )
