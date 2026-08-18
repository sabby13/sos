"""Wake-word detection.

openWakeWord ships a pretrained "hey jarvis" model, which is a fortunate coincidence for
this project - it means no training step and no API key for the always-listening path.
Detection runs locally on CPU at a few percent of one core.

If openWakeWord is not installed or its models cannot be fetched, we fall back to
push-to-talk rather than failing: pressing Enter is a worse experience than saying the
wake word, but it is a far better experience than a crash.
"""

from __future__ import annotations

import contextlib
import time
from typing import Protocol

from rich.markup import escape

from ..console import console, get_logger
from ..errors import SpeechError
from .audio import Microphone

log = get_logger("speech.wake")

# openWakeWord's pretrained set. Anything else is treated as a path to a custom .onnx.
BUILTIN_MODELS = {
    "hey_jarvis": "hey_jarvis_v0.1",
    "alexa": "alexa_v0.1",
    "hey_mycroft": "hey_mycroft_v0.1",
    "hey_rhasspy": "hey_rhasspy_v0.1",
    "timer": "timer_v0.1",
    "weather": "weather_v0.1",
}


class WakeWordDetector(Protocol):
    label: str

    def wait(self, mic: Microphone, timeout: float | None = None) -> bool:
        """Block until triggered. Returns False on timeout."""

    def reset(self) -> None: ...


class OpenWakeWordDetector:
    """Local neural wake-word spotting."""

    def __init__(self, wake_word: str = "hey_jarvis", threshold: float = 0.5) -> None:
        try:
            import numpy as np
            import openwakeword
            from openwakeword.model import Model
        except ImportError as exc:
            raise SpeechError(
                f"openWakeWord is not installed: {exc}",
                hint='Install the voice extras: pip install -e ".[voice]"',
            ) from exc

        self._np = np
        self.threshold = threshold
        self.label = wake_word.replace("_", " ")
        model_name = BUILTIN_MODELS.get(wake_word, wake_word)

        if wake_word in BUILTIN_MODELS:
            try:
                # No-op after the first run; the models land in the openwakeword package dir.
                openwakeword.utils.download_models(model_names=[model_name])
            except Exception as exc:
                raise SpeechError(
                    f"Could not download the '{wake_word}' wake-word model: {exc}",
                    hint="Check your connection, or run with --push-to-talk.",
                ) from exc

        try:
            self._model = Model(wakeword_models=[model_name], inference_framework="onnx")
        except TypeError:
            # openWakeWord < 0.5 spells the argument differently.
            self._model = Model(wakeword_model_paths=[model_name])
        except Exception as exc:
            raise SpeechError(f"Could not load the wake-word model: {exc}") from exc

        log.debug("wake word ready: %s (threshold %.2f)", model_name, threshold)

    def wait(self, mic: Microphone, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None and time.monotonic() > deadline:
                return False
            block = mic.read(timeout=1.0)
            if block is None:
                continue
            samples = self._np.frombuffer(block, dtype=self._np.int16)
            scores = self._model.predict(samples)
            best = max(scores.values()) if scores else 0.0
            if best >= self.threshold:
                log.debug("wake word fired at %.2f", best)
                self.reset()
                return True

    def reset(self) -> None:
        """Clear the model's internal buffers so one trigger cannot fire twice."""
        with contextlib.suppress(AttributeError):
            self._model.reset()


class PushToTalkDetector:
    """Fallback: Enter starts listening. Also what ``--push-to-talk`` selects."""

    label = "Enter"

    def wait(self, mic: Microphone, timeout: float | None = None) -> bool:
        console.print("[muted]press Enter to talk (Ctrl+C to quit)[/muted]", end="")
        try:
            input()
        except EOFError:
            return False
        mic.drain()  # discard whatever was captured while waiting
        return True

    def reset(self) -> None:
        return None


def build_detector(
    wake_word: str, threshold: float, *, push_to_talk: bool = False
) -> WakeWordDetector:
    """Pick a detector, degrading to push-to-talk with a visible warning."""
    if push_to_talk:
        return PushToTalkDetector()
    try:
        return OpenWakeWordDetector(wake_word, threshold)
    except SpeechError as exc:
        console.print(f"[warn]wake word unavailable:[/warn] {escape(exc.message)}")
        if exc.hint:
            console.print(f"[muted]{escape(exc.hint)}[/muted]")
        console.print("[muted]falling back to push-to-talk[/muted]")
        return PushToTalkDetector()
