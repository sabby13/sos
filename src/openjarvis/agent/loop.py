"""The agent loop.

One turn of ``Agent.ask`` is: send the conversation plus tool schemas to the model, and
if it asks for tools, run them (via the safety policy) and send the results back. Repeat
until the model replies with prose or we hit ``max_steps``.

The loop is deliberately synchronous and single-threaded. Voice input, the console, and
tool execution all happen on the same thread, which makes the confirmation prompts
trivially correct - there is no window where a tool runs while the user is being asked
about a different one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from ..config import Settings
from ..console import get_logger
from ..errors import JarvisError, SafetyError, ToolError
from ..llm.base import Completion, LLMProvider, Message, ToolCall
from ..platforms import get_adapter
from ..safety.confirm import Confirmer, ConsoleConfirmer
from ..safety.policy import SafetyPolicy, Verdict
from ..tools.base import ToolContext, ToolResult
from ..tools.registry import ToolRegistry, default_registry
from .prompts import build_system_prompt

log = get_logger("agent")


class TurnCancelled(Exception):
    """Raised when a desktop turn is cooperatively cancelled."""


@dataclass
class AgentEvents:
    """Hooks the UI layer fills in. All optional, all called synchronously."""

    on_thinking: Callable[[], None] | None = None
    on_tool_start: Callable[[str, str], None] | None = None  # (tool name, preview)
    on_tool_result: Callable[[str, ToolResult], None] | None = None
    on_blocked: Callable[[str, str], None] | None = None  # (tool name, reason)

    def fire(self, name: str, *args: object) -> None:
        handler = getattr(self, name, None)
        if handler is not None:
            handler(*args)


@dataclass
class TurnStats:
    steps: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


@dataclass
class Agent:
    settings: Settings
    provider: LLMProvider
    registry: ToolRegistry = field(default_factory=default_registry)
    policy: SafetyPolicy | None = None
    confirmer: Confirmer = field(default_factory=ConsoleConfirmer)
    events: AgentEvents = field(default_factory=AgentEvents)
    speak: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        self.policy = self.policy or SafetyPolicy(self.settings.safety)
        self.ctx = ToolContext(
            settings=self.settings,
            platform=get_adapter(),
            cwd=str(Path.cwd()),
            speak=self.speak,
        )
        self._system = Message.system(build_system_prompt(self.ctx.cwd))
        self._history: list[Message] = []
        self.last_stats = TurnStats()

    # ------------------------------------------------------------------ turn

    def ask(self, user_text: str, *, cancel_event: Event | None = None) -> str:
        """Run one user request to completion and return what to say back."""
        started = time.monotonic()
        stats = TurnStats()
        history_start = len(self._history)
        self._history.append(Message.user(user_text))

        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise TurnCancelled()

        try:
            schemas = self.registry.schemas()
            reply = ""

            for step in range(1, self.settings.llm.max_steps + 1):
                check_cancelled()
                stats.steps = step
                self.events.fire("on_thinking")

                completion = self.provider.complete([self._system, *self._history], schemas)
                check_cancelled()
                stats.prompt_tokens += completion.prompt_tokens
                stats.completion_tokens += completion.completion_tokens
                if completion.prompt_tokens or completion.completion_tokens:
                    log.debug(
                        "provider usage: prompt_tokens=%d completion_tokens=%d",
                        completion.prompt_tokens,
                        completion.completion_tokens,
                    )
                self._history.append(completion.message)

                if not completion.wants_tools:
                    reply = (completion.message.content or "").strip()
                    break

                stats.tool_calls += len(completion.message.tool_calls)
                early_reply = self._run_tool_calls(completion, check_cancelled)
                check_cancelled()
                if early_reply is not None:
                    reply = early_reply
                    break
            else:
                # Ran out of steps without the model settling on an answer.
                reply = (
                    "I got stuck working on that - I tried several steps without finishing. "
                    "Could you break it into smaller pieces?"
                )
                log.warning("hit max_steps=%d without a final answer", self.settings.llm.max_steps)

            stats.seconds = time.monotonic() - started
            self.last_stats = stats
            self._trim_history()
            return reply or "Done."
        except TurnCancelled:
            del self._history[history_start:]
            raise

    def _run_tool_calls(
        self,
        completion: Completion,
        check_cancelled: Callable[[], None] | None = None,
    ) -> str | None:
        """Execute every tool the model asked for.

        Returns the reply to end the turn with, or None to let the loop continue. Every
        call still gets a tool message appended even when an earlier one ended the turn -
        the API rejects an assistant turn whose tool calls are not all answered.
        """
        early_reply: str | None = None
        for call in completion.message.tool_calls:
            if check_cancelled is not None:
                check_cancelled()
            result = self._run_one(call)
            self._history.append(Message.tool(call.id, call.name, result.to_model_content()))
            self.events.fire("on_tool_result", call.name, result)
            if check_cancelled is not None:
                check_cancelled()
            if result.terminal and early_reply is None:
                # The tool's summary is written for the model; say something human instead.
                early_reply = "Okay, I've left that alone." if not result.ok else result.summary
        return early_reply

    def _run_one(self, call: ToolCall) -> ToolResult:
        """Validate, authorise, and execute a single tool call.

        Every failure path returns a ToolResult rather than raising: the model needs to
        *see* what went wrong so it can correct itself, and an exception here would throw
        away the whole turn.
        """
        try:
            tool = self.registry.get(call.name)
            raw_args = call.parsed_arguments()
            args = tool.parse_args(raw_args)
        except (ToolError, ValueError) as exc:
            log.info("rejected tool call %s: %s", call.name, exc)
            return ToolResult.failure(str(exc))

        assert self.policy is not None
        decision = self.policy.evaluate(tool, args, self.ctx)
        preview = tool.preview(args, self.ctx)

        if decision.verdict is Verdict.BLOCK:
            self.events.fire("on_blocked", tool.name, decision.reason)
            return ToolResult.failure(
                f"Blocked by the safety policy: {decision.reason}. Do not retry this."
            )

        if decision.verdict is Verdict.CONFIRM:
            approved = self.confirmer.confirm(f"{tool.name} ({tool.risk.value})", preview)
            if not approved:
                # terminal=True ends the turn here. A declined action means the user has
                # changed their mind, and letting the model keep planning around the
                # refusal is how assistants end up nagging.
                return ToolResult(
                    ok=False,
                    summary="The user declined that action. Do not retry it.",
                    terminal=True,
                )

        self.events.fire("on_tool_start", tool.name, preview)
        try:
            return tool.run(args, self.ctx)
        except (SafetyError, ToolError) as exc:
            log.info("%s failed: %s", tool.name, exc)
            return ToolResult.failure(str(exc))
        except FileNotFoundError as exc:
            return ToolResult.failure(f"That path does not exist: {exc}")
        except PermissionError as exc:
            return ToolResult.failure(
                f"Permission denied: {exc}. The file may be open in another program."
            )
        except JarvisError as exc:
            return ToolResult.failure(exc.message)
        except Exception as exc:
            log.exception("unexpected error in tool %s", tool.name)
            return ToolResult.failure(f"{tool.name} hit an unexpected error: {exc}")

    # --------------------------------------------------------------- history

    def _trim_history(self) -> None:
        """Keep the conversation bounded without orphaning tool messages.

        A ``role="tool"`` message is only valid if the assistant turn that requested it is
        still present, so we trim to a user-message boundary rather than a fixed count.
        """
        limit = self.settings.llm.history_turns * 4  # ~4 messages per exchange with tools
        if len(self._history) <= limit:
            return
        cutoff = len(self._history) - limit
        while cutoff < len(self._history) and self._history[cutoff].role != "user":
            cutoff += 1
        if cutoff < len(self._history):
            dropped = cutoff
            self._history = self._history[cutoff:]
            log.debug("trimmed %d old messages from history", dropped)

    def reset(self) -> None:
        """Forget the conversation but keep the machine context fresh."""
        self._history.clear()
        self.ctx.recent_paths.clear()
        self._system = Message.system(build_system_prompt(self.ctx.cwd))

    @property
    def history(self) -> list[Message]:
        return list(self._history)
