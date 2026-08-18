"""Provider-neutral message and tool-call types.

Groq and OpenAI both speak the OpenAI chat-completions wire format, so these mirror it
closely - but keeping our own types means a future provider that does *not* (Anthropic,
Ollama's native API, ...) can be added without touching the agent loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    """A single function invocation requested by the model."""

    id: str
    name: str
    arguments: str  # raw JSON string as emitted by the model

    def parsed_arguments(self) -> dict[str, Any]:
        """Decode ``arguments``, tolerating the empty string models sometimes send."""
        if not self.arguments or not self.arguments.strip():
            return {}
        try:
            value = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            raise ValueError(f"model emitted invalid JSON arguments: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"expected a JSON object of arguments, got {type(value).__name__}")
        return value


@dataclass(slots=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool"
    name: str | None = None  # tool name, for readability in logs

    def to_wire(self) -> dict[str, Any]:
        """Render into the OpenAI-compatible request shape."""
        msg: dict[str, Any] = {"role": self.role}
        # A tool-calling assistant turn may legitimately have no text content, but the
        # key must still be present for some servers.
        msg["content"] = self.content if self.content is not None else ""
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str | None, tool_calls: list[ToolCall] | None = None) -> Message:
        return cls(role="assistant", content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool(cls, tool_call_id: str, name: str, content: str) -> Message:
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)


@dataclass(slots=True)
class Completion:
    """One assistant turn: either prose, tool calls, or both."""

    message: Message
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.message.tool_calls)


class LLMProvider(Protocol):
    """The only surface the agent loop depends on."""

    name: str
    model: str

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion: ...

    def transcribe(self, audio: bytes, *, filename: str = "speech.wav") -> str: ...
