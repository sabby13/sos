"""Tool contract: how a capability describes itself, declares its risk, and runs.

A tool is a pydantic argument model plus a ``run`` method. The JSON schema handed to the
LLM is generated from that model, so the docstrings you write for parameters are literally
what the model reads - keep them short and behavioural.
"""

from __future__ import annotations

import textwrap
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from ..errors import ToolError
from ..risk import RiskLevel

__all__ = ["RiskLevel", "Tool", "ToolContext", "ToolResult"]


@dataclass(slots=True)
class ToolResult:
    """What a tool hands back.

    ``summary`` is one plain sentence - it is what gets spoken aloud and printed.
    ``data`` is the structured detail the model reasons over; it is JSON-serialised into
    the tool message, so keep it small enough to not blow the context window.
    """

    ok: bool
    summary: str
    data: Any = None
    # Set when the agent should stop and hand control back to the user immediately.
    terminal: bool = False

    @classmethod
    def success(cls, summary: str, data: Any = None, *, terminal: bool = False) -> ToolResult:
        return cls(ok=True, summary=summary, data=data, terminal=terminal)

    @classmethod
    def failure(cls, summary: str, data: Any = None) -> ToolResult:
        return cls(ok=False, summary=summary, data=data)

    def to_model_content(self) -> str:
        """Render for the ``role="tool"`` message."""
        import json

        payload: dict[str, Any] = {"ok": self.ok, "summary": self.summary}
        if self.data is not None:
            payload["data"] = self.data
        return json.dumps(payload, ensure_ascii=False, default=str)


@dataclass
class ToolContext:
    """Ambient state a tool may need. Passed to every ``run``."""

    settings: Any  # openjarvis.config.Settings - untyped here to avoid a circular import
    platform: Any  # openjarvis.platforms.base.PlatformAdapter
    # Working directory for relative paths, and the last paths the agent touched, so
    # "rename this file" / "open that folder" can resolve pronouns.
    cwd: str = ""
    recent_paths: list[str] = field(default_factory=list)
    speak: Any = None  # Callable[[str], None] | None - for tools that narrate progress

    def remember_path(self, path: str) -> None:
        if path in self.recent_paths:
            self.recent_paths.remove(path)
        self.recent_paths.insert(0, path)
        del self.recent_paths[8:]


ArgsT = TypeVar("ArgsT", bound=BaseModel)


class Tool(ABC, Generic[ArgsT]):
    """Base class for every capability the agent can invoke."""

    name: ClassVar[str]
    description: ClassVar[str]
    risk: ClassVar[RiskLevel] = RiskLevel.CAUTION
    args_model: ClassVar[type[BaseModel]]
    # Set False for tools that need an OS adapter the current platform lacks.
    available: ClassVar[bool] = True

    @abstractmethod
    def run(self, args: ArgsT, ctx: ToolContext) -> ToolResult:
        """Do the thing. Raise :class:`ToolError` for expected failures."""

    def preview(self, args: ArgsT, ctx: ToolContext) -> str:
        """One line shown to the user before a confirmation prompt.

        Override whenever the default (name + arguments) would be unclear about
        consequences - the user is deciding based on this sentence.
        """
        shown = {k: v for k, v in args.model_dump().items() if v is not None}
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in shown.items())})"

    # ---------------------------------------------------------------- schema

    def parse_args(self, raw: dict[str, Any]) -> ArgsT:
        try:
            return self.args_model.model_validate(raw)  # type: ignore[return-value]
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()
            )
            raise ToolError(f"Invalid arguments for {self.name}: {problems}") from exc

    @classmethod
    def to_schema(cls) -> dict[str, Any]:
        """The OpenAI/Groq function-tool definition for this tool."""
        params = _clean_schema(cls.args_model.model_json_schema())
        return {
            "type": "function",
            "function": {
                "name": cls.name,
                "description": textwrap.dedent(cls.description).strip(),
                "parameters": params,
            },
        }


# Keys that are JSON Schema metadata rather than useful instruction for the model.
_NOISE_KEYWORDS = {"title", "$defs", "additionalProperties"}
# Keys whose values are maps of *user-chosen names* to schemas. Their keys must never be
# treated as schema keywords - a tool parameter legitimately named "title" lives here.
_NAME_KEYED = {"properties", "patternProperties", "$defs", "definitions"}


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$defs`` and drop noise that only confuses smaller models.

    Pydantic emits ``$ref``/``$defs`` for enums and nested models, plus a ``title`` on
    every field. Llama-class models follow a flat, description-only schema far more
    reliably, so we resolve references and strip titles.
    """
    defs = schema.get("$defs", {})

    def clean(node: Any, depth: int = 0) -> Any:
        if depth > 12:  # cyclic model definition; stop rather than recurse forever
            return {}
        if isinstance(node, list):
            return [clean(item, depth + 1) for item in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            rest = {k: v for k, v in node.items() if k != "$ref"}
            target = defs.get(node["$ref"].rsplit("/", 1)[-1], {})
            # The sibling keys win: a field's own description beats the enum's.
            return {**clean(target, depth + 1), **clean(rest, depth + 1)}

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _NOISE_KEYWORDS:
                continue
            if key in _NAME_KEYED and isinstance(value, dict):
                # Recurse into the values only - these keys are parameter names.
                out[key] = {name: clean(sub, depth + 1) for name, sub in value.items()}
                continue
            # anyOf: [X, null] is how pydantic spells Optional[X]; flatten it so the
            # model sees a plain type rather than a union it may refuse to fill.
            if key == "anyOf" and isinstance(value, list):
                non_null = [v for v in value if isinstance(v, dict) and v.get("type") != "null"]
                if len(non_null) == 1:
                    out.update(clean(non_null[0], depth + 1))
                    continue
            out[key] = clean(value, depth + 1)
        return out

    cleaned = clean(schema)
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned
