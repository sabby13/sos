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

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any

from ..config import Settings
from ..console import get_logger
from ..diagnostics import sanitize_diagnostic
from ..errors import JarvisError, SafetyError, ToolError
from ..llm.base import Completion, LLMProvider, Message, ToolCall
from ..platforms import get_adapter
from ..risk import RiskLevel
from ..safety.confirm import Confirmer, ConsoleConfirmer
from ..safety.policy import SafetyPolicy, Verdict
from ..tools.apps import BUILTIN_ALIASES
from ..tools.base import ToolContext, ToolResult
from ..tools.registry import ToolRegistry, default_registry
from .prompts import build_system_prompt

log = get_logger("agent")
_LAUNCH_COMMAND = re.compile(
    r"(?:please\s+)?(?:open|launch|start|run)\s+(?:the\s+)?(.+)",
    re.IGNORECASE,
)
_URL_COMMAND = re.compile(
    r"(?:please\s+)?(?:open|go\s+to|visit)\s+((?:https?://|www\.)\S+)",
    re.IGNORECASE,
)
_DOMAIN_COMMAND = re.compile(
    r"(?:please\s+)?(?:open|go\s+to|visit)\s+([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+(?:/\S*)?)",
    re.IGNORECASE,
)
_SEARCH_COMMAND = re.compile(
    r"(?:please\s+)?(?:search\s+for|search\s+the\s+web\s+for|google|search)\s+(.+)",
    re.IGNORECASE,
)
_WINDOW_COMMAND = re.compile(
    r"(?:please\s+)?(close|minimize|maximize|restore|switch\s+to|focus)\s+(?:the\s+)?(.+?)(?:\s+window)?$",
    re.IGNORECASE,
)
_LIST_WINDOWS_COMMAND = re.compile(
    r"(?:please\s+)?(?:what\s+windows\s+are\s+open|what\s+do\s+i\s+have\s+open|list\s+windows|show\s+open\s+windows)$",
    re.IGNORECASE,
)
_LIST_APPS_COMMAND = re.compile(
    r"(?:please\s+)?(?:list\s+apps|list\s+applications|what\s+apps\s+are\s+installed)$",
    re.IGNORECASE,
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_WORD_TOKEN = re.compile(r"[a-z0-9]+")

# Words that cannot identify an outcome on their own: connectives and filler, plus URL
# scaffolding so "Opened www.pinterest.com." relates cleanly to "Your Pinterest search
# is open." Everything else - names, objects, numbers - is what distinguishes one
# statement from another.
_REPEAT_NOISE_WORDS = frozenset(
    [
        "a", "about", "all", "also", "an", "and", "any", "are", "as", "at", "been",
        "but", "by", "can", "could", "did", "do", "does", "doing", "for", "from",
        "get", "got", "had", "has", "have", "how", "in", "into", "is", "it", "its",
        "may", "me", "might", "must", "my", "no", "not", "now", "of", "on", "or",
        "out", "over", "she", "should", "so", "some", "than", "that", "the", "their",
        "them", "then", "there", "these", "they", "this", "those", "to", "too", "up",
        "us", "was", "we", "were", "what", "when", "where", "which", "while", "who",
        "why", "will", "with", "would", "you", "your", "com", "htm", "html", "http",
        "https", "net", "org", "www",
    ]
)


def _meaning_tokens(sentence: str) -> set[str]:
    tokens = _WORD_TOKEN.findall(sentence.lower())
    return {token for token in tokens if len(token) > 1 and token not in _REPEAT_NOISE_WORDS}


# Phrases that are essentially never part of a user-facing answer. Small reasoning
# models on Groq (gpt-oss) occasionally write their planning/self-critique text into
# the final channel - "Should respond: ..." - and that text must never be spoken or
# stored. This list deliberately leaves out soft planning phrases ("We need to...")
# that a genuine spoken answer can legitimately contain.
_META_LEAK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bshould (?:respond|say)\s*[:\-]",
        r"\bshould we also\b",
        r"\bthe response should be\b",
        r"\bthe previous (?:answer|reply)\b",
        r"\bthe assistant (?:already|will|should|has to|cannot|opened)\b",
        r"\bsearch results\s*\.{2,}",
    )
)

# Second-tier signal: the dump also discusses the conversation in the third person.
# Requiring both tiers keeps legitimate answers like "You should say: 'Thanks!'" spoken
# word-for-word while still catching the planning dumps.
_META_INTERNAL_REFERENCES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bthe (?:assistant|user|previous|response)\b",
        r"\bthe tool(?: result)?s?\b",
        r"\bwe (?:also )?need to\b",
    )
)

# When the model planned its answer inside a leaked dump, it states that answer in
# quotes. The recovered text is what the model actually intended to say.
_PLANNED_REPLY = re.compile(
    r"(?:should\s+(?:respond|say)|respond\s+with|reply\s+with)\s*[:\-]?\s*[\"']([^\"'\n]{4,300})[\"']",
    re.IGNORECASE,
)

# One corrective nudge when no planned answer can be recovered. Applied once per turn;
# the loop's own step limit bounds everything else.
_REPLY_CORRECTION = (
    "Your last reply was internal planning notes, not the answer for the user. "
    "Reply now with only the short user-facing answer."
)


def _reply_contains_meta_leak(reply: str) -> bool:
    """Two-tier detection: a planning phrase plus third-person internal referencing."""
    if not any(pattern.search(reply) for pattern in _META_LEAK_PATTERNS):
        return False
    return any(pattern.search(reply) for pattern in _META_INTERNAL_REFERENCES)


def _recover_planned_reply(reply: str) -> str | None:
    match = _PLANNED_REPLY.search(reply)
    if match:
        recovered = match.group(1).strip()
        if recovered and not _reply_contains_meta_leak(recovered):
            return recovered
        return None
    return None


def _echoes_tool_summary(sentence: str, tool_summaries: list[str]) -> bool:
    """True when a reply sentence merely parrots a tool result summary.

    The model receives ``{"ok": true, "summary": "Opened www.pinterest.com."}`` and is
    prone to repeating that summary inside its own reply before adding its own closer.
    The echo carries no information the summary did not, so it is recognized by its
    meaningful words being contained in the summary's. A richer sentence - one that
    adds the search terms, a follow-up, or a question - is never an echo.
    """
    tokens = _meaning_tokens(sentence)
    if not tokens:
        return False
    return any(tokens <= _meaning_tokens(summary) for summary in tool_summaries)


def _sentences_repeat(first: str, second: str) -> bool:
    """True when two sentences state the same outcome in different words.

    This is the paraphrase duplicate the user hears: "Pinterest is open with results
    for Nike shoes." followed by "Pinterest is open showing Nike shoes." Their
    distinctive words (pinterest, open, nike, shoes) coincide even though no wording
    does. Loosely related sentences - action plus a follow-up question, or a summary
    plus a genuinely new detail - do not share enough of their meaningful words.
    """
    first_tokens, second_tokens = _meaning_tokens(first), _meaning_tokens(second)
    if not first_tokens or not second_tokens:
        return False
    shared = first_tokens & second_tokens
    return len(shared) >= 2 and len(shared) / min(len(first_tokens), len(second_tokens)) >= 0.6


def _dedupe_reply_sentences(reply: str, tool_summaries: list[str] | None = None) -> str:
    """Collapse sentences that re-confirm what an earlier sentence already said.

    Small models occasionally echo a tool result's summary and then restate it in
    their own words, so the spoken confirmation plays twice. Echoed summaries are
    dropped only while another sentence in the reply still carries the turn; when the
    whole reply is the summary it stays, since it is the only confirmation. When a new
    sentence repeats a kept one, the new wording wins: it is the model's own closer
    rather than the parroted echo.
    """
    sentences = [part.strip() for part in _SENTENCE_BOUNDARY.split(reply.strip()) if part.strip()]
    if tool_summaries and len(sentences) > 1:
        without_echo = [
            sentence
            for sentence in sentences
            if not _echoes_tool_summary(sentence, tool_summaries)
        ]
        if without_echo:
            sentences = without_echo
    kept: list[str] = []
    for sentence in sentences:
        for index, existing in enumerate(kept):
            if _sentences_repeat(existing, sentence):
                kept[index] = sentence
                break
        else:
            kept.append(sentence)
    return " ".join(kept) if kept else reply.strip()


def _explicit_repeat_limit(text: str) -> int:
    lowered = text.casefold()
    if re.search(
        r"\b(?:don't|do not|never)\b.{0,30}\b(?:again|twice|repeat|\d+\s+times?)\b",
        lowered,
    ):
        return 1
    if re.search(r"\b(?:twice|two times|again|repeat(?: it)?)\b", lowered):
        return 2
    match = re.search(r"\b(\d{1,2})\s+times?\b", lowered)
    return max(1, min(int(match.group(1)), 10)) if match else 1


def _action_key(tool_name: str, arguments: dict[str, Any]) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(r"\s+", " ", value).strip().casefold()
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in sorted(value.items())}
        return value

    normalized = json.dumps(normalize(arguments), sort_keys=True, separators=(",", ":"))
    return f"{tool_name}:{normalized}"


def _duplicate_action_result(previous: ToolResult | None = None) -> ToolResult:
    completed = previous is None or previous.ok
    return ToolResult.success(
        (
            "This action was already completed for the request and was not repeated."
            if completed
            else "This action was already attempted for the request and was not repeated."
        ),
        {"duplicate": True, "previous_ok": completed},
    )


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
class _RequestToolExecutions:
    repeat_limit: int
    calls: dict[str, tuple[str, ToolResult]] = field(default_factory=dict)
    actions: dict[str, list[ToolResult]] = field(default_factory=dict)


@dataclass(frozen=True)
class MetaLeakRepair:
    """Outcome of keeping planning text out of a model reply.

    ``corrected`` carries the replacement text (planned answer or tool-summary
    fallback). ``needs_correction`` asks the loop for one corrective model round.
    Neither set: the reply was clean and passes through unchanged.
    """

    corrected: str | None = None
    needs_correction: bool = False


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

    def ask(
        self,
        user_text: str,
        *,
        cancel_event: Event | None = None,
        fast_path: bool = False,
    ) -> str:
        """Run one user request to completion and return what to say back."""
        started = time.monotonic()
        stats = TurnStats()
        history_start = len(self._history)
        self._history.append(Message.user(user_text))
        executions = _RequestToolExecutions(_explicit_repeat_limit(user_text))
        playback_statuses: list[str] = []

        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise TurnCancelled()

        try:
            check_cancelled()
            command = self._fast_path_command(user_text) if fast_path else None
            if command is not None:
                tool_name, arguments = command
                call = ToolCall(
                    id=uuid.uuid4().hex,
                    name=tool_name,
                    arguments=json.dumps(arguments),
                )
                self._history.append(Message.assistant(None, [call]))
                result = self._run_one(call, executions)
                check_cancelled()
                self._history.append(Message.tool(call.id, call.name, result.to_model_content()))
                self.events.fire("on_tool_result", call.name, result)
                reply = (
                    "Okay, I've left that alone."
                    if result.terminal and not result.ok
                    else result.summary
                    if result.ok
                    else f"I couldn't complete that: {result.summary}"
                )
                self._history.append(Message.assistant(reply))
                stats.steps = 1
                stats.tool_calls = 1
                stats.seconds = time.monotonic() - started
                self.last_stats = stats
                self._trim_history()
                return reply

            schemas = self.registry.schemas()
            reply = ""
            correction_index: int | None = None

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
                if completion.wants_tools:
                    self._history.append(Message.assistant(None, completion.message.tool_calls))
                else:
                    reply = (completion.message.content or "").strip()
                    repair = self._repair_meta_leak(reply, history_start, correction_index)
                    if repair.corrected is not None:
                        reply = repair.corrected
                    elif repair.needs_correction:
                        # Ask the model once for the user-facing answer. The leaked text
                        # is dropped, not stored, so the conversation never learns it.
                        correction_index = len(self._history)
                        self._history.append(Message.user(_REPLY_CORRECTION))
                        continue
                    reply = _dedupe_reply_sentences(reply, self._turn_tool_summaries(history_start))
                    if playback_statuses:
                        reply = playback_statuses[-1]
                    self._history.append(Message.assistant(reply))
                    break

                stats.tool_calls += len(completion.message.tool_calls)
                early_reply = self._run_tool_calls(
                    completion,
                    check_cancelled,
                    executions,
                    playback_statuses,
                )
                check_cancelled()
                if early_reply is not None:
                    reply = early_reply
                    break
            else:
                if playback_statuses:
                    reply = playback_statuses[-1]
                    self._history.append(Message.assistant(reply))
                else:
                    # Ran out of steps without the model settling on an answer.
                    reply = (
                        "I got stuck working on that - I tried several steps without finishing. "
                        "Could you break it into smaller pieces?"
                    )
                    log.warning(
                        "hit max_steps=%d without a final answer", self.settings.llm.max_steps
                    )

            stats.seconds = time.monotonic() - started
            self.last_stats = stats
            if correction_index is not None:
                # The one-shot corrective instruction is not part of the conversation:
                # it only re-aims the model within this turn.
                del self._history[correction_index]
            self._trim_history()
            return reply or "Done."
        except TurnCancelled:
            del self._history[history_start:]
            raise

    def _repair_meta_leak(
        self,
        reply: str,
        history_start: int,
        correction_index: int | None,
    ) -> MetaLeakRepair:
        """Keep planning text a model leaked into its final channel out of the reply.

        gpt-oss on Groq occasionally writes its analysis into the user-facing message:
        "Search results ... ... Should respond: \"Pinterest search for charcoal
        drawings opened.\" ... The previous answer had weird formatting." Detection uses
        planning phrases plus third-person internals ("the assistant", "the response"),
        so genuine answers survive untouched. Recovery order:
        the planned answer the dump quotes in full, else one corrective re-ask, else
        the turn's last successful tool summary - and the leaked text is never spoken
        nor stored in the history.
        """
        if not _reply_contains_meta_leak(reply):
            return MetaLeakRepair()
        planned = _recover_planned_reply(reply)
        if planned is not None:
            log.warning(
                "model leaked planning text into the reply; answering with the planned text"
            )
            return MetaLeakRepair(corrected=planned)
        if correction_index is None:
            log.warning(
                "model leaked planning text into the reply; asking once for the user-facing answer"
            )
            return MetaLeakRepair(needs_correction=True)
        ok_summaries = self._turn_tool_summaries(history_start, ok_only=True)
        log.warning(
            "model leaked planning text twice; falling back to the tool result summary"
        )
        return MetaLeakRepair(corrected=ok_summaries[-1] if ok_summaries else "")

    def _turn_tool_summaries(self, history_start: int, *, ok_only: bool = False) -> list[str]:
        """The summaries the tools of the current turn wrote into the history.

        The final reply is shaped against these so a model that parrots a summary or
        restates it in other words does not make the user hear the same outcome twice.
        With ``ok_only`` the list keeps successful actions only, which is what a
        last-resort fallback confirmation should be built from.
        """
        summaries: list[str] = []
        for message in self._history[history_start:]:
            if message.role != "tool" or not message.content:
                continue
            try:
                payload = json.loads(message.content)
            except ValueError:
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str):
                continue
            if ok_only and not payload.get("ok", False):
                continue
            summaries.append(payload["summary"])
        return summaries

    def _fast_path_command(self, text: str) -> tuple[str, dict[str, Any]] | None:
        """Recognize safe explicit desktop commands to save LLM round trips and token quotas."""
        command = text.strip().rstrip(".!?").strip()

        # 1. URL opening (full URL or domain)
        url_match = _URL_COMMAND.fullmatch(command) or _DOMAIN_COMMAND.fullmatch(command)
        if url_match and "open_url" in self.registry:
            url = url_match.group(1).rstrip(".,!?;:")
            if url:
                return "open_url", {"url": url}

        # 2. Web search
        search_match = _SEARCH_COMMAND.fullmatch(command)
        if search_match and "web_search" in self.registry:
            query = search_match.group(1).strip()
            if query:
                return "web_search", {"query": query}

        # 3. Application launching
        known_aliases = {name.casefold() for name in BUILTIN_ALIASES}
        known_aliases.update(name.strip().casefold() for name in self.settings.tools.app_aliases)

        launch_match = _LAUNCH_COMMAND.fullmatch(command)
        if launch_match and "launch_application" in self.registry:
            app = launch_match.group(1).strip()
            app_clean = re.sub(r"\s+(?:app|application)$", "", app, flags=re.I).strip()
            if app_clean.casefold() in known_aliases:
                return "launch_application", {"app": app_clean}
            if app.casefold() in known_aliases:
                return "launch_application", {"app": app}

        # 4. Window control (close, minimize, maximize, restore, switch to / focus)
        window_match = _WINDOW_COMMAND.fullmatch(command)
        if window_match and "control_window" in self.registry:
            verb = window_match.group(1).lower()
            action = "focus" if ("switch" in verb or "focus" in verb) else verb
            target = window_match.group(2).strip()
            target_clean = re.sub(r"\s+(?:app|application)$", "", target, flags=re.I).strip()
            if target_clean:
                return "control_window", {"title": target_clean, "action": action}

        # 5. List windows
        if _LIST_WINDOWS_COMMAND.fullmatch(command) and "list_windows" in self.registry:
            return "list_windows", {}

        # 6. List applications
        if _LIST_APPS_COMMAND.fullmatch(command) and "list_applications" in self.registry:
            return "list_applications", {}

        return None

    def _run_tool_calls(
        self,
        completion: Completion,
        check_cancelled: Callable[[], None] | None = None,
        executions: _RequestToolExecutions | None = None,
        playback_statuses: list[str] | None = None,
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
            result = self._run_one(call, executions)
            self._history.append(Message.tool(call.id, call.name, result.to_model_content()))
            self.events.fire("on_tool_result", call.name, result)
            if (
                call.name == "play_youtube"
                and result.ok
                and isinstance(result.data, dict)
                and not result.data.get("duplicate")
                and playback_statuses is not None
            ):
                playback_statuses.append(result.summary)
            if check_cancelled is not None:
                check_cancelled()
            if result.terminal and early_reply is None:
                # The tool's summary is written for the model; say something human instead.
                early_reply = "Okay, I've left that alone." if not result.ok else result.summary
        return early_reply

    def _run_one(
        self,
        call: ToolCall,
        executions: _RequestToolExecutions | None = None,
    ) -> ToolResult:
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
            log.info("rejected tool call %s: %s", call.name, sanitize_diagnostic(str(exc)))
            return ToolResult.failure(str(exc))

        action_key = ""
        if executions is not None and tool.risk is not RiskLevel.SAFE:
            action_key = _action_key(tool.name, raw_args)
            prior_call = executions.calls.get(call.id)
            if prior_call is not None:
                if prior_call[0] == action_key:
                    return _duplicate_action_result(prior_call[1])
                return ToolResult.failure(
                    "A duplicate tool-call ID was ignored to prevent an unintended action."
                )
            previous_actions = executions.actions.get(action_key, [])
            if len(previous_actions) >= executions.repeat_limit:
                return _duplicate_action_result(previous_actions[-1])

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
            result = tool.run(args, self.ctx)
        except (SafetyError, ToolError) as exc:
            log.info("%s failed: %s", tool.name, sanitize_diagnostic(str(exc)))
            result = ToolResult.failure(str(exc))
        except FileNotFoundError as exc:
            result = ToolResult.failure(f"That path does not exist: {exc}")
        except PermissionError as exc:
            result = ToolResult.failure(
                f"Permission denied: {exc}. The file may be open in another program."
            )
        except JarvisError as exc:
            result = ToolResult.failure(exc.message)
        except Exception as exc:  # noqa: BLE001 -- tool failures must not discard the turn.
            log.error(
                "unexpected error in tool %s (%s): %s",
                tool.name,
                type(exc).__name__,
                sanitize_diagnostic(str(exc)),
            )
            result = ToolResult.failure(
                f"{tool.name} hit an unexpected error: {sanitize_diagnostic(str(exc))}"
            )

        if executions is not None and action_key:
            executions.calls[call.id] = (action_key, result)
            executions.actions.setdefault(action_key, []).append(result)
        return result

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
