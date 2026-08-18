"""Shared fixtures.

Nothing here touches the network, the microphone, or the real file system outside
pytest's tmp_path - the whole suite runs offline in about a second.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from openjarvis import config as config_module
from openjarvis.config import Settings
from openjarvis.llm.base import Completion, Message, ToolCall
from openjarvis.platforms.base import AppInfo, PlatformAdapter, WindowInfo
from openjarvis.tools.base import ToolContext


class FakePlatform(PlatformAdapter):
    """An OS that does nothing but remember what it was asked to do."""

    name = "fake"

    def __init__(self) -> None:
        self.launched: list[tuple[str, list[str]]] = []
        self.opened_urls: list[str] = []
        self.revealed: list[str] = []
        self.focused: list[int] = []
        self.trashed: list[str] = []
        self.apps = [
            AppInfo("Visual Studio Code", r"C:\apps\Code.lnk", "start-menu"),
            AppInfo("Google Chrome", r"C:\apps\chrome.lnk", "start-menu"),
            AppInfo("Notepad", r"C:\Windows\notepad.exe", "path"),
        ]
        self.windows = [
            WindowInfo(1, "README.md - Visual Studio Code", "Code.exe"),
            WindowInfo(2, "OpenJarvis - Google Chrome", "chrome.exe"),
            WindowInfo(3, "Untitled - Notepad", "notepad.exe", minimized=True),
        ]

    def list_apps(self) -> list[AppInfo]:
        return list(self.apps)

    def launch(self, target: str, args: list[str] | None = None) -> int | None:
        self.launched.append((target, list(args or [])))
        return 4242

    def which(self, command: str) -> str | None:
        return rf"C:\bin\{command}.exe" if command in {"code", "chrome", "notepad"} else None

    def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def focus_window(self, handle: int) -> bool:
        self.focused.append(handle)
        return True

    def close_window(self, handle: int) -> bool:
        self.windows = [w for w in self.windows if w.handle != handle]
        return True

    def set_window_state(self, handle: int, state: str) -> bool:
        return any(w.handle == handle for w in self.windows)

    def reveal(self, path) -> bool:  # type: ignore[no-untyped-def]
        self.revealed.append(str(path))
        return True

    def open_with_default_app(self, path) -> bool:  # type: ignore[no-untyped-def]
        self.revealed.append(str(path))
        return True

    def open_url(self, url: str, browser: str | None = None) -> bool:
        self.opened_urls.append(url)
        return True

    def move_to_trash(self, path) -> bool:  # type: ignore[no-untyped-def]
        # Report failure so tools fall back to a real delete inside tmp_path, which is
        # what we want to assert on.
        self.trashed.append(str(path))
        return False


@dataclass
class FakeProvider:
    """Replays a scripted list of completions instead of calling an API."""

    name: str = "fake"
    model: str = "fake-model"
    script: list[Completion] = field(default_factory=list)
    seen: list[list[Message]] = field(default_factory=list)

    def complete(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> Completion:
        self.seen.append(list(messages))
        if not self.script:
            return Completion(message=Message.assistant("Done."), finish_reason="stop")
        return self.script.pop(0)

    def transcribe(self, audio: bytes, *, filename: str = "speech.wav") -> str:
        return "transcribed"


def text_completion(content: str) -> Completion:
    return Completion(message=Message.assistant(content), finish_reason="stop")


def tool_completion(name: str, arguments: str, call_id: str = "call_1") -> Completion:
    return Completion(
        message=Message.assistant(None, [ToolCall(id=call_id, name=name, arguments=arguments)]),
        finish_reason="tool_calls",
    )


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Cut every test off from the developer's real machine.

    Without this, ``Settings.load`` picks up the repository's own ``.env`` and
    ``~/.openjarvis/config.yaml``, so the suite passes or fails depending on whose laptop
    it runs on - and a test asserting "no API key" fails for anyone who has one.
    """
    home = tmp_path / ".openjarvis"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "APP_DIR", home)
    monkeypatch.setattr(config_module, "CONFIG_PATH", home / "config.yaml")
    for key in ("GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key in list(os.environ):
        if key.startswith("OPENJARVIS_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Default settings, sandboxed to tmp_path so no test can escape."""
    cfg = Settings()
    cfg.safety.allowed_roots = [str(tmp_path)]
    cfg.data_dir = tmp_path / ".openjarvis"
    return cfg


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def ctx(settings, platform, tmp_path) -> ToolContext:
    return ToolContext(settings=settings, platform=platform, cwd=str(tmp_path))
