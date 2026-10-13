"""The agent loop, driven by a scripted provider instead of a real model."""

from __future__ import annotations

import json
import logging
from threading import Event

from conftest import FakeProvider, text_completion, tool_completion
from openjarvis.agent.loop import Agent, TurnCancelled
from openjarvis.errors import ProviderError
from openjarvis.llm.base import Completion, Message
from openjarvis.safety.confirm import AutoConfirmer, DenyConfirmer
from openjarvis.tools.registry import ToolRegistry, default_registry


def build_agent(settings, platform, tmp_path, script, confirmer=None) -> Agent:
    agent = Agent(
        settings=settings,
        provider=FakeProvider(script=list(script)),
        registry=default_registry(),
        confirmer=confirmer or AutoConfirmer(),
    )
    agent.ctx.platform = platform
    agent.ctx.cwd = str(tmp_path)
    return agent


def tool_messages(agent: Agent) -> list[dict]:
    return [json.loads(m.content) for m in agent.history if m.role == "tool"]


def test_plain_answer_needs_no_tools(settings, platform, tmp_path):
    agent = build_agent(settings, platform, tmp_path, [text_completion("Hello.")])
    assert agent.ask("hi") == "Hello."


def test_cancelled_agent_turn_discards_its_conversation(settings, platform, tmp_path):
    cancellation = Event()

    class CancellingProvider:
        name = "fake"
        model = "fake-model"

        def complete(self, messages, tools=None):
            cancellation.set()
            return text_completion("This answer must not escape.")

        def transcribe(self, audio, *, filename="speech.wav"):
            return ""

    agent = Agent(settings=settings, provider=CancellingProvider())
    agent.ctx.platform = platform
    agent.ctx.cwd = str(tmp_path)

    try:
        agent.ask("cancel this request", cancel_event=cancellation)
    except TurnCancelled:
        pass
    else:
        raise AssertionError("expected cooperative cancellation")

    assert agent.history == []


def test_tool_call_executes_then_the_model_summarises(settings, platform, tmp_path):
    agent = build_agent(
        settings, platform, tmp_path,
        [
            tool_completion("create_folder", json.dumps({"path": str(tmp_path / "Projects")})),
            text_completion("Created the Projects folder."),
        ],
    )
    reply = agent.ask("create a folder called Projects")
    assert (tmp_path / "Projects").is_dir()
    assert reply == "Created the Projects folder."
    assert tool_messages(agent)[0]["ok"] is True


def test_tool_results_are_fed_back_to_the_model(settings, platform, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    agent = build_agent(
        settings, platform, tmp_path,
        [
            tool_completion("list_directory", json.dumps({"path": str(tmp_path)})),
            text_completion("You have one file."),
        ],
    )
    agent.ask("what's in this folder?")
    # The second request must contain the tool's output, or the model is answering blind.
    second_request = agent.provider.seen[1]
    assert any(m.role == "tool" and "a.txt" in (m.content or "") for m in second_request)


def test_provider_failure_after_tool_execution_does_not_replay_action(
    settings, platform, tmp_path
):
    class ProviderFailsAfterLaunch:
        name = "fake"
        model = "fake-model"

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return tool_completion("launch_application", '{"app": "Chrome"}')
            raise ProviderError("rate limit")

        def transcribe(self, audio, *, filename="speech.wav"):
            return "transcribed"

    provider = ProviderFailsAfterLaunch()
    agent = Agent(
        settings=settings,
        provider=provider,
        registry=default_registry(),
        confirmer=AutoConfirmer(),
    )
    agent.ctx.platform = platform
    agent.ctx.cwd = str(tmp_path)

    try:
        agent.ask("open Chrome")
    except ProviderError as exc:
        assert exc.message == "rate limit"
    else:
        raise AssertionError("expected provider failure after the launch")

    assert provider.calls == 2
    assert len(platform.launched) == 1


def test_usage_debug_log_uses_provider_metadata_without_logging_transcript(
    settings, platform, tmp_path, caplog
):
    usage = Completion(
        message=Message.assistant("Done."),
        prompt_tokens=17,
        completion_tokens=4,
    )
    agent = build_agent(settings, platform, tmp_path, [usage])

    with caplog.at_level(logging.DEBUG, logger="openjarvis.agent"):
        agent.ask("private spoken text")

    assert "prompt_tokens=17 completion_tokens=4" in caplog.text
    assert "private spoken text" not in caplog.text


def test_a_failing_tool_does_not_end_the_turn(settings, platform, tmp_path):
    """The model must see the error and get a chance to correct itself."""
    agent = build_agent(
        settings, platform, tmp_path,
        [
            tool_completion("read_text_file", json.dumps({"path": str(tmp_path / "missing.txt")})),
            text_completion("That file doesn't exist."),
        ],
    )
    reply = agent.ask("read missing.txt")
    assert tool_messages(agent)[0]["ok"] is False
    assert reply == "That file doesn't exist."


def test_invalid_arguments_come_back_as_a_recoverable_error(settings, platform, tmp_path):
    agent = build_agent(
        settings, platform, tmp_path,
        [
            tool_completion("create_folder", '{"wrong_field": 1}'),
            text_completion("Let me try that differently."),
        ],
    )
    agent.ask("make a folder")
    message = tool_messages(agent)[0]
    assert message["ok"] is False
    assert "Invalid arguments" in message["summary"]


def test_unknown_tool_names_are_reported_not_raised(settings, platform, tmp_path):
    agent = build_agent(
        settings, platform, tmp_path,
        [tool_completion("hack_the_mainframe", "{}"), text_completion("I can't do that.")],
    )
    agent.ask("do something impossible")
    assert "no tool called" in tool_messages(agent)[0]["summary"]


def test_malformed_json_arguments_are_survivable(settings, platform, tmp_path):
    agent = build_agent(
        settings, platform, tmp_path,
        [tool_completion("create_folder", "{not json at all"), text_completion("Retrying.")],
    )
    agent.ask("make a folder")
    assert tool_messages(agent)[0]["ok"] is False


def test_declining_a_confirmation_stops_the_turn(settings, platform, tmp_path):
    victim = tmp_path / "precious.txt"
    victim.write_text("keep me")
    agent = build_agent(
        settings, platform, tmp_path,
        [
            tool_completion("delete_item", json.dumps({"path": str(victim)})),
            text_completion("This should never be reached."),
        ],
        confirmer=DenyConfirmer(),
    )
    reply = agent.ask("delete precious.txt")
    assert victim.exists(), "a declined confirmation must not delete anything"
    assert "left that alone" in reply
    assert agent.last_stats.steps == 1


def test_safe_tools_never_prompt(settings, platform, tmp_path):
    """If read-only tools prompted, voice mode would be unusable."""

    class ExplodingConfirmer:
        def confirm(self, title: str, detail: str) -> bool:
            raise AssertionError(f"should not have been asked about {title}")

    agent = build_agent(
        settings, platform, tmp_path,
        [
            tool_completion("list_directory", json.dumps({"path": str(tmp_path)})),
            text_completion("Empty."),
        ],
        confirmer=ExplodingConfirmer(),
    )
    assert agent.ask("what's here?") == "Empty."


def test_max_steps_stops_a_runaway_loop(settings, platform, tmp_path):
    settings.llm.max_steps = 3
    script = [
        tool_completion("list_directory", json.dumps({"path": str(tmp_path)}), f"c{i}")
        for i in range(10)
    ]
    agent = build_agent(settings, platform, tmp_path, script)
    reply = agent.ask("loop forever")
    assert agent.last_stats.steps == 3
    assert "stuck" in reply


def test_parallel_tool_calls_all_get_answered(settings, platform, tmp_path):
    """The API rejects an assistant turn whose tool calls are not all responded to."""
    from openjarvis.llm.base import Completion, Message, ToolCall

    parallel = Completion(
        message=Message.assistant(
            None,
            [
                ToolCall("c1", "create_folder", json.dumps({"path": str(tmp_path / "one")})),
                ToolCall("c2", "create_folder", json.dumps({"path": str(tmp_path / "two")})),
            ],
        ),
        finish_reason="tool_calls",
    )
    agent = build_agent(settings, platform, tmp_path, [parallel, text_completion("Both made.")])
    agent.ask("make two folders")

    requested = {"c1", "c2"}
    answered = {m.tool_call_id for m in agent.history if m.role == "tool"}
    assert answered == requested
    assert (tmp_path / "one").is_dir() and (tmp_path / "two").is_dir()


def test_reset_clears_the_conversation(settings, platform, tmp_path):
    script = [text_completion("Hi."), text_completion("Hi again.")]
    agent = build_agent(settings, platform, tmp_path, script)
    agent.ask("hello")
    assert agent.history
    agent.reset()
    assert agent.history == []


def test_history_trimming_keeps_tool_messages_with_their_turn(settings, platform, tmp_path):
    """Orphaned tool messages are a 400 from the API, so trimming must land on a user turn."""
    settings.llm.history_turns = 1
    script = []
    for _ in range(6):
        script.append(tool_completion("list_directory", json.dumps({"path": str(tmp_path)})))
        script.append(text_completion("ok"))
    agent = build_agent(settings, platform, tmp_path, script)
    for _ in range(3):
        agent.ask("look around")

    assert agent.history[0].role == "user"
    seen_calls = set()
    for message in agent.history:
        if message.role == "assistant":
            seen_calls.update(tc.id for tc in message.tool_calls)
        if message.role == "tool":
            assert message.tool_call_id in seen_calls, "tool message lost its assistant turn"


def test_empty_registry_still_answers(settings, platform, tmp_path):
    agent = Agent(
        settings=settings,
        provider=FakeProvider(script=[text_completion("No tools needed.")]),
        registry=ToolRegistry(),
        confirmer=AutoConfirmer(),
    )
    assert agent.ask("hello") == "No tools needed."
