"""Line-oriented IPC bridge for the HelloSienna desktop client."""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from collections.abc import Callable
from typing import Any, TextIO

from .config import API_KEY_ENV, Settings
from .console import get_logger
from .errors import JarvisError, SpeechError

log = get_logger("desktop")


class DesktopBridge:
    """Own one OpenJarvis agent and one active microphone/audio turn."""

    def __init__(self, output: TextIO | None = None) -> None:
        self.output = output or sys.stdout
        self.settings = Settings.load()
        self._output_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._confirmation_lock = threading.Lock()
        self._confirmations: dict[str, tuple[threading.Event, list[bool]]] = {}
        self._agent = None
        self._speaker = None
        self._microphone = None
        self._voice_lock = threading.Lock()
        self._stopping = threading.Event()
        self._turn_cancel = threading.Event()
        self._active_request_id: str | None = None
        self._active_lock = threading.Lock()

    def emit(self, event_type: str, **fields: Any) -> None:
        message = {"type": event_type, **fields}
        with self._output_lock:
            self.output.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")))
            self.output.write("\n")
            self.output.flush()

    def run(self, input_stream: TextIO | None = None) -> None:
        """Read requests until shutdown or EOF; stdout is reserved for JSON events."""
        source = input_stream or sys.stdin
        self.emit("ready", backend="openjarvis")
        for line in source:
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self.emit("error", message=f"Invalid desktop request: {exc.msg}")
                continue
            if not isinstance(request, dict):
                self.emit("error", message="Desktop request must be a JSON object.")
                continue
            request_id = request.get("id")
            request_type = request.get("type")
            if request_type == "shutdown":
                self.shutdown()
                return
            if request_type == "cancel":
                self._cancel_turn(request.get("requestId"))
                continue
            if request_type == "get-settings":
                self.emit(
                    "settings",
                    requestId=request_id,
                    settings=self._public_settings(),
                )
                continue
            if request_type == "configure":
                self._configure(request, request_id)
                continue
            if request_type == "confirmation":
                self._resolve_confirmation(request)
                continue
            if request_type not in {"ask", "voice"}:
                self.emit(
                    "error",
                    requestId=request_id,
                    message=f"Unsupported desktop request: {request_type!r}",
                )
                continue
            if not isinstance(request_id, str) or not request_id:
                self.emit("error", message="Desktop requests require a non-empty id.")
                continue
            if request_type == "ask" and (
                not isinstance(request.get("text"), str) or not request["text"].strip()
            ):
                self.emit("error", requestId=request_id, message="Message text is required.")
                continue
            if not self._turn_lock.acquire(blocking=False):
                self.emit(
                    "error",
                    requestId=request_id,
                    message="Sienna is already handling a request.",
                )
                continue
            self._turn_cancel.clear()
            with self._active_lock:
                self._active_request_id = request_id
            worker = threading.Thread(
                target=self._handle_turn,
                args=(request_type, request_id, request.get("text", "")),
                daemon=True,
                name="openjarvis-desktop-turn",
            )
            worker.start()

        self.shutdown()

    def shutdown(self) -> None:
        self._stopping.set()
        microphone = self._microphone
        if microphone is not None:
            microphone.stop()
        if self._speaker is not None:
            self._speaker.stop()
        with self._confirmation_lock:
            for event, answer in self._confirmations.values():
                answer[:] = [False]
                event.set()

    def _cancel_turn(self, request_id: Any) -> None:
        with self._active_lock:
            if not isinstance(request_id, str) or request_id != self._active_request_id:
                self.emit(
                    "cancel-error",
                    requestId=request_id if isinstance(request_id, str) else None,
                    message="That request is no longer active.",
                )
                return
            self._turn_cancel.set()

        if self._speaker is not None:
            self._speaker.stop()
        self.emit("state", state="cancelling", requestId=request_id)

    def _public_settings(self) -> dict[str, Any]:
        provider = self.settings.llm.provider
        return {
            "provider": provider,
            "model": self.settings.llm.resolved_model(),
            "apiKeyConfigured": bool(os.environ.get(API_KEY_ENV[provider])),
            "wakeWord": self.settings.speech.wake_word,
            "ttsEngine": self.settings.speech.tts_engine,
            "ttsVoice": self.settings.speech.resolved_tts_voice(),
        }

    def _configure(self, request: dict[str, Any], request_id: Any) -> None:
        config = request.get("settings")
        if not isinstance(config, dict):
            self.emit("error", requestId=request_id, message="Settings must be an object.")
            return
        engine = config.get("ttsEngine")
        voice = config.get("ttsVoice")
        fallback = config.get("ttsFallback")
        if "ttsVoice" in config and voice is not None and not isinstance(voice, str):
            self.emit(
                "error",
                requestId=request_id,
                message="Speech voice must be a string or null.",
            )
            return
        if fallback is not None and fallback not in {"edge", "system", "openai", "groq", "none"}:
            self.emit("error", requestId=request_id, message="Unsupported speech fallback.")
            return
        if engine is not None:
            if engine not in {"edge", "system", "none"}:
                self.emit("error", requestId=request_id, message="Unsupported speech engine.")
                return
            self.settings.speech.tts_engine = engine
        if "ttsVoice" in config:
            self.settings.speech.tts_voice = voice
        if fallback is not None:
            self.settings.speech.tts_fallback = fallback
        if engine is not None or "ttsVoice" in config or fallback is not None:
            self._speaker = None
        self.emit("settings", requestId=request_id, settings=self._public_settings())

    def _resolve_confirmation(self, request: dict[str, Any]) -> None:
        confirmation_id = request.get("confirmationId")
        approved = request.get("approved")
        if not isinstance(confirmation_id, str) or not isinstance(approved, bool):
            self.emit("error", message="Invalid safety confirmation response.")
            return
        with self._confirmation_lock:
            pending = self._confirmations.get(confirmation_id)
        if pending is None:
            self.emit("error", message="The safety confirmation is no longer pending.")
            return
        event, answer = pending
        answer[:] = [approved]
        event.set()

    def _confirm(self, title: str, detail: str) -> bool:
        confirmation_id = str(uuid.uuid4())
        event = threading.Event()
        answer = [False]
        with self._confirmation_lock:
            self._confirmations[confirmation_id] = (event, answer)
        self.emit(
            "confirmation",
            confirmationId=confirmation_id,
            title=title,
            detail=detail,
        )
        try:
            while not event.wait(timeout=0.1):
                if self._stopping.is_set() or self._turn_cancel.is_set():
                    return False
            return answer[0] and not self._stopping.is_set() and not self._turn_cancel.is_set()
        finally:
            with self._confirmation_lock:
                self._confirmations.pop(confirmation_id, None)

    def _get_agent(self):
        if self._agent is not None:
            return self._agent

        from .agent.loop import Agent, AgentEvents
        from .llm.openai_compatible import build_provider
        from .tools.registry import default_registry

        events = AgentEvents(
            on_thinking=lambda: self.emit("state", state="processing"),
            on_tool_start=lambda name, preview: self.emit(
                "tool", state="executing", name=name, preview=preview
            ),
            on_tool_result=lambda name, result: self.emit(
                "tool-result", name=name, ok=result.ok, summary=result.summary
            ),
            on_blocked=lambda name, reason: self.emit(
                "tool-blocked", name=name, reason=reason
            ),
        )
        self._agent = Agent(
            settings=self.settings,
            provider=build_provider(self.settings),
            registry=default_registry(),
            confirmer=_DesktopConfirmer(self._confirm),
            events=events,
            speak=self._speak,
        )
        self._agent.ctx.speak = self._speak
        return self._agent

    def _get_speaker(self):
        if self._speaker is None:
            from .speech.tts import build_speaker

            self._speaker = build_speaker(self.settings)
        return self._speaker

    def _speak(self, text: str) -> None:
        if self._turn_cancel.is_set():
            return
        speaker = self._get_speaker()
        if self._turn_cancel.is_set():
            return
        self.emit("state", state="speaking")
        try:
            speaker.speak(text, block=True)
        except SpeechError as exc:
            self.emit("speech-error", message=exc.message)

    def _handle_turn(self, request_type: str, request_id: str, text: str) -> None:
        final_state = "idle"
        try:
            if request_type == "voice":
                text = self._capture_voice()
                if self._turn_cancel.is_set():
                    return
                if not text:
                    self.emit("error", requestId=request_id, message="No speech was detected.")
                    final_state = "error"
                    return
                self.emit("transcript", requestId=request_id, text=text)
            if self._turn_cancel.is_set():
                return
            self.emit("state", state="processing")
            response = self._get_agent().ask(text, cancel_event=self._turn_cancel)
            if self._turn_cancel.is_set():
                return
            self.emit("reply", requestId=request_id, text=response)
            if request_type == "voice" and self.settings.speech.tts_engine != "none":
                self._speak(response)
        except JarvisError as exc:
            if self._turn_cancel.is_set():
                return
            self.emit(
                "error",
                requestId=request_id,
                message=exc.message,
                hint=exc.hint,
            )
            final_state = "error"
        except Exception as exc:
            if self._turn_cancel.is_set():
                return
            log.exception("unexpected error handling desktop request %s", request_id)
            self.emit("error", requestId=request_id, message=str(exc))
            final_state = "error"
        finally:
            cancelled = self._turn_cancel.is_set()
            with self._active_lock:
                self._active_request_id = None
            if cancelled:
                self.emit(
                    "cancelled",
                    requestId=request_id,
                    message=(
                        "Cancellation was acknowledged. An in-flight provider call or desktop "
                        "action may have finished before it could stop; completed actions "
                        "cannot be undone."
                    ),
                )
                self.emit("state", state="idle", requestId=request_id)
            else:
                self.emit("state", state=final_state, requestId=request_id)
            self._turn_lock.release()

    def _capture_voice(self) -> str:
        from .speech.audio import Microphone, record_utterance
        from .speech.stt import Transcriber

        self.emit("state", state="listening")
        microphone = Microphone(
            sample_rate=self.settings.speech.sample_rate,
            device=self.settings.speech.input_device,
        )
        with self._voice_lock:
            self._microphone = microphone
        try:
            if self._turn_cancel.is_set():
                return ""
            with microphone:
                utterance = record_utterance(
                    microphone,
                    silence_ms=self.settings.speech.silence_ms,
                    min_utterance_ms=self.settings.speech.min_utterance_ms,
                    max_seconds=self.settings.speech.max_utterance_s,
                    sensitivity=self.settings.speech.vad_sensitivity,
                    start_timeout_s=15,
                    cancel_event=self._turn_cancel,
                )
            if self._turn_cancel.is_set():
                return ""
            if utterance is None:
                return ""
            self.emit("state", state="transcribing")
            transcriber = Transcriber(self.settings)
            transcript = transcriber.transcribe(
                utterance.to_wav(self.settings.speech.sample_rate)
            )
            return "" if self._turn_cancel.is_set() else transcript
        finally:
            with self._voice_lock:
                self._microphone = None


class _DesktopConfirmer:
    def __init__(self, confirm: Callable[[str, str], bool]) -> None:
        self._confirm = confirm

    def confirm(self, title: str, detail: str) -> bool:
        return self._confirm(title, detail)


def main() -> None:
    DesktopBridge().run()


if __name__ == "__main__":
    main()
