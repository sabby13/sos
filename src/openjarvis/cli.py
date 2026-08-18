"""Command line interface.

``jarvis`` with no arguments starts voice mode, because that is the point of the project.
Everything else is a subcommand: ``chat`` for the text REPL, ``ask`` for one-shot,
and a handful of inspection commands (``doctor``, ``tools``, ``devices``, ``voices``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import CONFIG_PATH, Settings
from .console import console, say_error, say_jarvis, say_tool, setup_logging
from .errors import JarvisError

app = typer.Typer(
    name="jarvis",
    help="OpenJarvis - an open-source, voice-first AI desktop agent.",
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
)

# --------------------------------------------------------------------- options

ProviderOpt = Annotated[
    str | None, typer.Option("--provider", "-p", help="LLM provider: groq or openai.")
]
ModelOpt = Annotated[str | None, typer.Option("--model", "-m", help="Override the model id.")]
ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to a config.yaml.")
]
SafetyOpt = Annotated[
    str | None,
    typer.Option("--safety", help="Safety mode: tiered (default), strict, or auto."),
]
YesOpt = Annotated[
    bool, typer.Option("--yes", "-y", help="Approve every confirmation. Use with care.")
]
DryRunOpt = Annotated[
    bool, typer.Option("--dry-run", help="Decline every confirmation - watch without executing.")
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")]


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    provider: ProviderOpt = None,
    model: ModelOpt = None,
    config: ConfigOpt = None,
    safety: SafetyOpt = None,
    yes: YesOpt = False,
    dry_run: DryRunOpt = False,
    verbose: VerboseOpt = False,
    version: Annotated[
        bool, typer.Option("--version", help="Print the version and exit.")
    ] = False,
) -> None:
    if version:
        console.print(f"OpenJarvis {__version__}")
        raise typer.Exit()

    settings = Settings.load(config)
    settings.override(provider=provider, model=model, safety_mode=safety)
    if verbose:
        settings.log_level = "DEBUG"
    setup_logging(settings.log_level)

    ctx.obj = CliState(settings=settings, yes=yes, dry_run=dry_run)

    if ctx.invoked_subcommand is None:
        _run_voice(ctx.obj, push_to_talk=False, no_speech=False, greeting=None)


class CliState:
    def __init__(self, settings: Settings, yes: bool, dry_run: bool) -> None:
        self.settings = settings
        self.yes = yes
        self.dry_run = dry_run

    def confirmer(self, voice_session: object | None = None):  # type: ignore[no-untyped-def]
        from .safety.confirm import AutoConfirmer, ConsoleConfirmer, DenyConfirmer, VoiceConfirmer

        if self.dry_run:
            return DenyConfirmer()
        if self.yes:
            return AutoConfirmer()
        if voice_session is not None:
            return VoiceConfirmer(
                speak=voice_session.say, listen=voice_session.listen_once
            )
        return ConsoleConfirmer()

    def build_agent(self, speak=None):  # type: ignore[no-untyped-def]
        from .agent.loop import Agent
        from .llm.openai_compatible import build_provider
        from .tools.registry import default_registry

        return Agent(
            settings=self.settings,
            provider=build_provider(self.settings),
            registry=default_registry(),
            confirmer=self.confirmer(),
            events=_console_events(),
            speak=speak,
        )


def _console_events():  # type: ignore[no-untyped-def]
    from .agent.loop import AgentEvents
    from .tools.base import ToolResult

    def on_tool_start(name: str, preview: str) -> None:
        say_tool(preview)

    def on_tool_result(name: str, result: ToolResult) -> None:
        if not result.ok:
            console.print(f"  [danger]x[/danger] [muted]{escape(result.summary)}[/muted]")

    def on_blocked(name: str, reason: str) -> None:
        console.print(f"  [danger]blocked[/danger] [muted]{name}: {escape(reason)}[/muted]")

    return AgentEvents(
        on_tool_start=on_tool_start, on_tool_result=on_tool_result, on_blocked=on_blocked
    )


# -------------------------------------------------------------------- commands


@app.command()
def voice(
    ctx: typer.Context,
    push_to_talk: Annotated[
        bool,
        typer.Option("--push-to-talk", "-k", help="Press Enter to talk, no wake word."),
    ] = False,
    no_speech: Annotated[
        bool, typer.Option("--no-speech", help="Listen, but print replies instead of speaking.")
    ] = False,
    greeting: Annotated[
        str | None, typer.Option("--greeting", help="Say this on startup.")
    ] = None,
) -> None:
    """Start voice mode: wake word, listen, act, speak. [dim](default)[/dim]"""
    _run_voice(ctx.obj, push_to_talk=push_to_talk, no_speech=no_speech, greeting=greeting)


def _run_voice(
    state: CliState, *, push_to_talk: bool, no_speech: bool, greeting: str | None
) -> None:
    from .speech.session import VoiceSession

    if no_speech:
        state.settings.speech.tts_engine = "none"

    agent = state.build_agent()
    session = VoiceSession(agent=agent, settings=state.settings, push_to_talk=push_to_talk)
    # Now that the session exists, confirmations can be spoken rather than typed.
    agent.confirmer = state.confirmer(voice_session=session)
    agent.speak = session.say
    agent.ctx.speak = session.say

    session.run(greeting=greeting)


@app.command()
def chat(ctx: typer.Context) -> None:
    """Text conversation in the terminal - no microphone involved."""
    state: CliState = ctx.obj
    agent = state.build_agent()

    console.print()
    console.print("[jarvis]OpenJarvis[/jarvis] [muted]text mode[/muted]")
    console.print(
        f"[muted]  {agent.provider.name}/{agent.provider.model}   "
        f"{len(agent.registry)} tools   safety: {agent.policy.describe()}[/muted]"
    )
    console.print("[muted]  /reset clears the conversation, /exit quits[/muted]")
    console.print()

    while True:
        try:
            line = console.input("[user]you[/user] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            continue
        if line in {"/exit", "/quit", "exit", "quit"}:
            break
        if line == "/reset":
            agent.reset()
            say_tool("conversation reset")
            continue
        if line == "/tools":
            _print_tools(agent.registry)
            continue

        try:
            reply = agent.ask(line)
        except JarvisError as exc:
            say_error(exc.message, exc.hint)
            continue
        say_jarvis(reply)
        stats = agent.last_stats
        console.print(
            f"[muted]  {stats.steps} steps, {stats.tool_calls} tool calls, "
            f"{stats.seconds:.1f}s[/muted]"
        )


@app.command()
def ask(
    ctx: typer.Context,
    request: Annotated[list[str], typer.Argument(help="What you want done.")],
) -> None:
    """Run one request and exit. [dim]jarvis ask \"open my downloads folder\"[/dim]"""
    state: CliState = ctx.obj
    agent = state.build_agent()
    reply = agent.ask(" ".join(request))
    say_jarvis(reply)


@app.command()
def tools(ctx: typer.Context) -> None:
    """List every tool the agent can call, with its risk level."""
    from .tools.registry import default_registry

    _print_tools(default_registry())


def _print_tools(registry) -> None:  # type: ignore[no-untyped-def]
    table = Table(box=None, pad_edge=False)
    table.add_column("tool", style="bold")
    table.add_column("risk")
    table.add_column("what it does", style="dim", overflow="fold")

    colours = {"safe": "green", "caution": "yellow", "dangerous": "red"}
    for name, risk, description in registry.summary():
        table.add_row(name, f"[{colours[risk]}]{risk}[/{colours[risk]}]", escape(description))
    console.print(table)
    console.print(
        f"\n[muted]{len(registry)} tools. "
        "safe runs silently, caution runs but is logged, dangerous asks first.[/muted]"
    )


@app.command()
def models(
    ctx: typer.Context,
    provider: ProviderOpt = None,
) -> None:
    """List the models your API key can actually reach, so you can pick one."""
    state: CliState = ctx.obj
    from .config import DEFAULT_LLM_MODELS, DEFAULT_STT_MODELS
    from .llm.openai_compatible import build_audio_provider

    name = provider or state.settings.llm.provider
    available = build_audio_provider(state.settings, name).list_models()

    defaults = {DEFAULT_LLM_MODELS.get(name), DEFAULT_STT_MODELS.get(name)}
    table = Table(box=None, pad_edge=False)
    table.add_column("model", style="bold")
    table.add_column("", style="dim")
    for model in available:
        table.add_row(model, "default" if model in defaults else "")
    console.print(table)
    console.print(f"\n[muted]{len(available)} models on {name}. Select one with --model.[/muted]")


@app.command()
def devices() -> None:
    """List microphones, so you can pick one for speech.input_device."""
    from .speech.audio import list_input_devices

    table = Table(box=None, pad_edge=False)
    table.add_column("index", style="bold")
    table.add_column("name")
    table.add_column("channels", style="dim")
    for index, name, channels in list_input_devices():
        table.add_row(str(index), name, str(channels))
    console.print(table)


@app.command()
def voices(
    prefix: Annotated[
        str, typer.Option("--prefix", help="Locale prefix, e.g. en- or en-GB.")
    ] = "en-",
) -> None:
    """List the free Edge neural voices you can set as speech.tts_voice."""
    import asyncio

    from .speech.tts import list_edge_voices

    table = Table(box=None, pad_edge=False)
    table.add_column("voice", style="bold")
    table.add_column("gender", style="dim")
    for name, gender in asyncio.run(list_edge_voices(prefix)):
        table.add_row(name, gender)
    console.print(table)


@app.command()
def mic(
    ctx: typer.Context,
    seconds: Annotated[float, typer.Option("--seconds", "-s", help="How long to record.")] = 4.0,
    device: Annotated[
        int | None, typer.Option("--device", "-d", help="Input device index from `jarvis devices`.")
    ] = None,
    transcribe: Annotated[
        bool, typer.Option("--transcribe/--no-transcribe", help="Send the audio for transcription.")
    ] = True,
) -> None:
    """Record from the microphone and report what was heard.

    The fastest way to diagnose "the wake word never fires": if the level here reads
    zero, the selected input device is not your actual microphone.
    """
    state: CliState = ctx.obj
    from .speech.audio import Microphone, rms

    speech = state.settings.speech
    if device is not None:
        speech.input_device = device
    label = speech.input_device if speech.input_device is not None else "system default"
    console.print(f"[muted]recording {seconds:.0f}s from device {label}...[/muted]")

    console.print("[muted]say something...[/muted]")
    chunks: list[bytes] = []
    peak = 0.0
    any_signal = False
    with Microphone(sample_rate=speech.sample_rate, device=speech.input_device) as microphone:
        blocks = int(seconds * speech.sample_rate / microphone.block_size)
        for _ in range(blocks):
            block = microphone.read(timeout=2.0)
            if block is None:
                break
            chunks.append(block)
            level = rms(block)
            peak = max(peak, level)
            any_signal = any_signal or level > 0.0
            # A meter in dBFS, because a live-but-quiet mic sits around 0.0003 and
            # rounding that to four decimal places makes a working device look dead.
            bar = "#" * min(int(level * 200), 40)
            console.print(f"  [muted]{_dbfs(level):>7}[/muted] {bar}")

    console.print(f"\npeak: [bold]{_dbfs(peak)}[/bold] [muted](linear {peak:.5f})[/muted]")

    # Speech is roughly -26 dBFS and up; a live but silent room sits near -70.
    if not any_signal or peak <= 0.00001:
        console.print(
            Panel(
                "The device returned digital silence - not even room noise. It is muted, "
                "disconnected, or blocked by Windows microphone privacy settings.\n\n"
                "Run [bold]jarvis devices[/bold], find your real microphone, and set it:\n"
                "  speech:\n    input_device: <index>\n\n"
                "in [bold]~/.openjarvis/config.yaml[/bold] (create it with "
                "[bold]jarvis config init[/bold]).",
                title="[danger]no signal at all[/danger]",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)
    if peak < 0.02:
        console.print(
            "[warn]The mic is live, but nothing loud enough to be speech was heard.[/warn]\n"
            "[muted]If you were speaking, this device is too quiet - raise its level in "
            "Windows sound settings, or lower speech.vad_sensitivity.[/muted]"
        )
    else:
        console.print("[ok]Levels look healthy - speech will be detected.[/ok]")

    if transcribe and chunks:
        from .speech.audio import Utterance
        from .speech.stt import Transcriber

        pcm = b"".join(chunks)
        utterance = Utterance(pcm=pcm, seconds=len(pcm) / (speech.sample_rate * 2), peak_level=peak)
        with console.status("[muted]transcribing...[/muted]"):
            text = Transcriber(state.settings).transcribe(utterance.to_wav(speech.sample_rate))
        console.print(f"heard: [bold]{escape(text) if text else '(nothing recognised)'}[/bold]")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check that everything OpenJarvis needs is present and working."""
    state: CliState = ctx.obj
    settings = state.settings
    rows: list[tuple[str, bool, str]] = []

    available = settings.available_providers()
    rows.append(
        ("API keys", bool(available), ", ".join(available) or "none found - see .env.example")
    )

    configured = settings.llm.provider
    rows.append((
        "LLM",
        configured in available,
        f"{configured}/{settings.llm.resolved_model()}"
        + ("" if configured in available else "  (no API key)"),
    ))
    stt = settings.speech.stt_provider
    rows.append((
        "Transcription",
        stt in available,
        f"{stt}/{settings.speech.resolved_stt_model()}"
        + ("" if stt in available else "  (no API key)"),
    ))

    from .speech.audio import HAVE_AUDIO

    rows.append((
        "Microphone",
        HAVE_AUDIO,
        _describe_mic() if HAVE_AUDIO else 'not installed - pip install -e ".[voice]"',
    ))
    rows.append(_check_import("Wake word", "openwakeword", settings.speech.wake_word))
    rows.append(_check_import("Speech output", _tts_module(settings), settings.speech.tts_engine))

    from .platforms import get_adapter
    from .tools.registry import default_registry

    adapter = get_adapter()
    rows.append(("Platform", adapter.name != "generic", f"{adapter.name} ({sys.platform})"))
    registry = default_registry()
    rows.append(("Tools", len(registry) > 0, f"{len(registry)} registered"))
    config_note = "" if CONFIG_PATH.exists() else "  (using defaults)"
    rows.append(("Config", True, f"{CONFIG_PATH}{config_note}"))

    table = Table(box=None, pad_edge=False)
    table.add_column("")
    table.add_column("check", style="bold")
    table.add_column("detail", style="dim", overflow="fold")
    for label, ok, detail in rows:
        table.add_row("[ok]OK[/ok]" if ok else "[danger]--[/danger]", label, escape(detail))
    console.print(table)

    if not available:
        console.print(
            Panel(
                "No API key found. Copy [bold].env.example[/bold] to [bold].env[/bold] and add "
                "either GROQ_API_KEY or OPENAI_API_KEY.\n"
                "Groq is free to start and much faster for transcription.",
                title="[warn]next step[/warn]",
                border_style="yellow",
            )
        )


def _dbfs(level: float) -> str:
    """Linear 0..1 level as decibels relative to full scale."""
    import math

    if level <= 0.0:
        return "-inf dB"
    return f"{20 * math.log10(level):.0f} dB"


def _tts_module(settings: Settings) -> str:
    return {"edge": "edge_tts", "system": "pyttsx3"}.get(settings.speech.tts_engine, "openai")


def _check_import(label: str, module: str, detail: str) -> tuple[str, bool, str]:
    import importlib.util

    ok = importlib.util.find_spec(module) is not None
    return (label, ok, detail if ok else f"{module} not installed")


def _describe_mic() -> str:
    from .speech.audio import list_input_devices

    try:
        found = list_input_devices()
    except JarvisError as exc:
        return exc.message
    if not found:
        return "no input devices found"
    return f"{len(found)} input(s), default: {found[0][1]}"


config_app = typer.Typer(help="Inspect and create the config file.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the effective configuration, after YAML and environment are merged."""
    console.print(ctx.obj.settings.to_yaml())


@config_app.command("path")
def config_path() -> None:
    """Print where the config file lives."""
    console.print(str(CONFIG_PATH))


@config_app.command("init")
def config_init(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write the current settings to the config file so you can edit them."""
    if CONFIG_PATH.exists() and not force:
        console.print(f"[warn]{CONFIG_PATH} already exists.[/warn] Use --force to overwrite.")
        raise typer.Exit(code=1)
    written = ctx.obj.settings.save()
    console.print(f"[ok]wrote[/ok] {written}")


def main() -> None:
    """Entry point. Turns expected failures into one clean line."""
    try:
        app()
    except JarvisError as exc:
        say_error(f"error: {exc.message}", exc.hint)
        sys.exit(1)
    except KeyboardInterrupt:
        console.print()
        sys.exit(130)


if __name__ == "__main__":
    main()
