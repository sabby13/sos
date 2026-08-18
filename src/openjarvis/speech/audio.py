"""Microphone capture and end-of-utterance detection.

The VAD here is energy-based rather than a neural model, on purpose. It has no extra
dependency, adds no latency, and calibrates to whatever room you are in by tracking the
noise floor continuously. It is not as sharp as Silero at picking speech out of loud
background noise - if you need that, this is the one file you would swap.

The pre-roll buffer matters more than it looks: wake-word detection eats the first few
hundred milliseconds after you start talking, so without it "hey Jarvis, open Chrome"
transcribes as "pen Chrome".
"""

from __future__ import annotations

import io
import queue
import wave
from collections import deque
from dataclasses import dataclass

from ..console import get_logger
from ..errors import SpeechError

log = get_logger("speech.audio")

SAMPLE_WIDTH = 2  # int16
CHANNELS = 1
# openWakeWord wants exactly 1280 samples (80 ms) at 16 kHz, so that is the block size
# for the whole pipeline.
BLOCK_SIZE = 1280

try:  # optional: only needed for voice mode
    import numpy as np
    import sounddevice as sd

    HAVE_AUDIO = True
except Exception as exc:  # noqa: BLE001 - PortAudio failures surface as odd exception types
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]
    HAVE_AUDIO = False
    _IMPORT_ERROR = exc


def require_audio() -> None:
    if not HAVE_AUDIO:
        raise SpeechError(
            f"Audio capture is unavailable: {_IMPORT_ERROR}",
            hint='Install the voice extras: pip install -e ".[voice]"',
        )


@dataclass(slots=True)
class Utterance:
    """One recorded stretch of speech."""

    pcm: bytes
    seconds: float
    peak_level: float

    def to_wav(self, sample_rate: int = 16000) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(CHANNELS)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(sample_rate)
            handle.writeframes(self.pcm)
        return buffer.getvalue()


class Microphone:
    """A continuously running 16 kHz mono input stream.

    Opened once and kept open for the whole session - re-opening a PortAudio stream per
    utterance costs a few hundred milliseconds and sometimes loses the first block.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        device: int | str | None = None,
        block_size: int = BLOCK_SIZE,
        preroll_blocks: int = 8,  # 640 ms of history
    ) -> None:
        require_audio()
        self.sample_rate = sample_rate
        self.device = device
        self.block_size = block_size
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stream: object | None = None
        self._preroll: deque = deque(maxlen=preroll_blocks)

    def __enter__(self) -> Microphone:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._stream is not None:
            return

        def callback(indata, frames, time_info, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                log.debug("audio input status: %s", status)
            try:
                self._queue.put_nowait(bytes(indata))
            except queue.Full:
                # Dropping a block beats blocking PortAudio's realtime thread.
                log.debug("audio queue full, dropped a block")

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                device=self.device,
                dtype="int16",
                channels=CHANNELS,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            raise SpeechError(
                f"Could not open the microphone: {exc}",
                hint="Run `jarvis devices` to list inputs, then set speech.input_device.",
            ) from exc

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, timeout: float = 1.0) -> bytes | None:
        """Next block of PCM, or None if the mic went quiet for ``timeout`` seconds."""
        try:
            block = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        self._preroll.append(block)
        return block

    def drain(self) -> None:
        """Discard buffered audio - call before listening so we don't hear ourselves."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._preroll.clear()

    def preroll(self) -> bytes:
        return b"".join(self._preroll)


def rms(block: bytes) -> float:
    """Root-mean-square level of one PCM block, normalised to roughly 0..1."""
    samples = np.frombuffer(block, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples**2)) / 32768.0)


def record_utterance(
    mic: Microphone,
    *,
    silence_ms: int = 900,
    min_utterance_ms: int = 400,
    max_seconds: float = 20.0,
    sensitivity: float = 2.5,
    start_timeout_s: float = 6.0,
    include_preroll: bool = True,
) -> Utterance | None:
    """Record until the speaker stops. Returns None if they never started.

    ``sensitivity`` is a multiplier over the measured noise floor. Higher means the room
    has to get louder before it counts as speech - raise it in a noisy room, lower it if
    quiet speech gets cut off.
    """
    block_seconds = mic.block_size / mic.sample_rate
    silence_blocks = max(int((silence_ms / 1000) / block_seconds), 1)
    min_blocks = max(int((min_utterance_ms / 1000) / block_seconds), 1)
    max_blocks = int(max_seconds / block_seconds)
    start_deadline_blocks = int(start_timeout_s / block_seconds)

    collected: list[bytes] = []
    if include_preroll:
        collected.append(mic.preroll())

    noise_floor = 0.005  # sane starting guess for a quiet room
    peak = 0.0
    speaking = False
    quiet_run = 0
    waited = 0

    while len(collected) < max_blocks:
        block = mic.read(timeout=2.0)
        if block is None:
            if speaking:
                break  # stream stalled mid-utterance; keep what we have
            return None

        level = rms(block)
        peak = max(peak, level)

        if not speaking:
            # Track the floor downward while waiting, so a quiet room stays sensitive.
            noise_floor = min(noise_floor, level) * 0.9 + level * 0.1
            waited += 1
            if level > max(noise_floor * sensitivity, 0.01):
                speaking = True
                collected.append(block)
                continue
            if waited > start_deadline_blocks:
                return None
            continue

        collected.append(block)
        if level > max(noise_floor * sensitivity, 0.008):
            quiet_run = 0
        else:
            quiet_run += 1
            if quiet_run >= silence_blocks and len(collected) >= min_blocks:
                break

    pcm = b"".join(collected)
    seconds = len(pcm) / (mic.sample_rate * SAMPLE_WIDTH)
    if seconds < min_utterance_ms / 1000:
        return None
    return Utterance(pcm=pcm, seconds=seconds, peak_level=peak)


def list_input_devices() -> list[tuple[int, str, int]]:
    """(index, name, channels) for every device that can record."""
    require_audio()
    devices = []
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] > 0:
            devices.append((index, info["name"], info["max_input_channels"]))
    return devices
