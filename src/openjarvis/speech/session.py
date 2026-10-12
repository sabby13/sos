"""The voice loop.

    wait for "hey jarvis"  ->  record until you stop talking  ->  transcribe
         ->  run the agent  ->  speak the reply  ->  listen again briefly

The follow-up window is what makes it feel like a conversation rather than a vending
machine: for a few seconds after each reply, Sienna keeps listening without needing the
wake word, so "open Chrome" / "now go to GitHub" works as two sentences.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..agent.loop import Agent
from ..config import Settings
from ..console import console, get_logger, say_error, say_jarvis, say_tool, say_user
from ..errors import JarvisError, ProviderError, SpeechError
from .audio import Microphone, record_utterance
from .stt import Transcriber, strip_wake_word
from .tts import Speaker, build_speaker
from .wakeword import WakeWordDetector, build_detector

log = get_logger("speech.session")

# Said when the wake word fires but the request could not be understood.
_DIDNT_CATCH = "Sorry, I didn't catch that."

# Spoken commands handled locally - no round trip to the model.
_STOP_WORDS = {"stop", "stop listening", "goodbye", "good bye", "exit", "quit", "shut down",
               "never mind", "nevermind", "cancel", "go to sleep"}
_RESET_WORDS = {"reset", "start over", "forget that", "new conversation", "clear"}


@dataclass
class VoiceSession:
    agent: Agent
    settings: Settings
    push_to_talk: bool = False

    def __post_init__(self) -> None:
        speech = self.settings.speech
        self.mic = Microphone(sample_rate=speech.sample_rate, device=speech.input_device)
        self.detector: WakeWordDetector = build_detector(
            speech.wake_word, speech.wake_threshold, push_to_talk=self.push_to_talk
        )
        self.transcriber = Transcriber(self.settings)
        self.speaker: Speaker = build_speaker(self.settings)
        self._running = False

    # ------------------------------------------------------------------ loop

    def run(self, greeting: str | None = None) -> None:
        """Listen until Ctrl+C or a spoken stop word."""
        speech = self.settings.speech
        self._running = True

        with self.mic:
            self._banner()
            if greeting:
                self.say(greeting)

            while self._running:
                try:
                    if not self._await_trigger():
                        continue
                    text = self._capture_command()
                    if text is None:
                        continue
                    if self._handle_local_command(text):
                        continue

                    say_user(text)
                    reply = self._respond(text)
                    if reply:
                        say_jarvis(reply)
                        self.say(reply)

                    self._follow_up_window(speech.followup_window_s)

                except KeyboardInterrupt:
                    console.print()
                    self.say("Goodbye.")
                    break
                except ProviderError as exc:
                    # Network blips should not end the session.
                    say_error(f"provider error: {exc.message}")
                    self.say("I couldn't reach the model. I'll keep listening.")
                except JarvisError as exc:
                    say_error(exc.message, exc.hint)
                    self.say("Something went wrong with that one.")

        self._running = False

    def stop(self) -> None:
        self._running = False

    # -------------------------------------------------------------- pipeline

    def _await_trigger(self) -> bool:
        with console.status(f"[muted]listening for '{self.detector.label}'...[/muted]"):
            return self.detector.wait(self.mic)

    def _capture_command(self, *, prompt: bool = True) -> str | None:
        """Record and transcribe one utterance. None means nothing usable was heard."""
        speech = self.settings.speech
        if prompt:
            console.print("[jarvis]Sienna[/jarvis] [muted](listening; speak now)[/muted]")

        utterance = record_utterance(
            self.mic,
            silence_ms=speech.silence_ms,
            min_utterance_ms=speech.min_utterance_ms,
            max_seconds=speech.max_utterance_s,
            sensitivity=speech.vad_sensitivity,
        )
        if utterance is None:
            log.debug("no speech captured")
            return None

        with console.status("[muted]transcribing...[/muted]"):
            text = self.transcriber.transcribe(utterance.to_wav(speech.sample_rate))
        if not text:
            return None
        return strip_wake_word(text, speech.wake_word)

    def _respond(self, text: str) -> str:
        with console.status("[muted]thinking...[/muted]"):
            return self.agent.ask(text)

    def _follow_up_window(self, seconds: float) -> None:
        """Listen for one more command without the wake word."""
        if seconds <= 0:
            return
        self.mic.drain()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            text = self._capture_command(prompt=False)
            if text is None:
                return
            if self._handle_local_command(text):
                return
            say_user(text)
            reply = self._respond(text)
            if reply:
                say_jarvis(reply)
                self.say(reply)
            deadline = time.monotonic() + seconds

    def _handle_local_command(self, text: str) -> bool:
        """Intercept the few commands not worth a model round trip."""
        normalised = text.lower().strip(" .!?")
        if normalised in _STOP_WORDS:
            self.say("Going to sleep.")
            self._running = False
            return True
        if normalised in _RESET_WORDS:
            self.agent.reset()
            say_tool("conversation reset")
            self.say("Starting fresh.")
            return True
        return False

    # --------------------------------------------------------------- helpers

    def say(self, text: str) -> None:
        """Speak, and never let a synthesis failure take down the session."""
        try:
            self.speaker.speak(text, block=True)
        except SpeechError as exc:
            log.warning("could not speak: %s", exc)
        finally:
            # Whatever leaked into the mic while the speaker was playing is not a command.
            self.mic.drain()

    def listen_once(self) -> str | None:
        """One utterance, no wake word. Used by the spoken confirmation prompt."""
        return self._capture_command(prompt=False)

    def _banner(self) -> None:
        console.print()
        console.print("[jarvis]Sienna[/jarvis] [muted]voice mode[/muted]")
        console.print(
            f"[muted]  wake: {self.detector.label}   "
            f"brain: {self.agent.provider.name}/{self.agent.provider.model}   "
            f"ears: {self.transcriber.provider_name}/{self.transcriber.model}   "
            f"voice: {self.speaker.name}[/muted]"
        )
        console.print(f"[muted]  safety: {self.agent.policy.describe()}[/muted]")
        console.print("[muted]  say 'stop' or press Ctrl+C to quit[/muted]")
        console.print()
