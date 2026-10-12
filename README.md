<p align="center">
  <img src="assets/hellosienna-hero.png"
       alt="HelloSienna — Voice-Activated Desktop Assistant"
       width="100%">
</p>

<h1 align="center">HelloSienna</h1>

<p align="center">
  <strong>A voice for your digital world.</strong>
  <br>
  An experimental, voice-activated Windows desktop assistant built on OpenJarvis.
</p>

## Overview

HelloSienna is an experimental proof of concept using the OpenJarvis voice and desktop
automation engine. **Sienna** is the assistant's conversational identity. The current
activation phrase remains **"Hey Jarvis"** because the installed wake-word model is trained
for that phrase. **"HelloSienna" is a planned future activation phrase** and is not supported
until a compatible wake-word model has been trained and tested.

The assistant captures speech, transcribes it, uses an LLM to select desktop tools, applies
the configured safety policy, and speaks its response. The existing OpenJarvis package,
CLI commands, and configuration identifiers are retained.

## Current Features

- Voice activation with the existing local openWakeWord "Hey Jarvis" model.
- Microphone capture with voice activity detection and speech transcription.
- Groq and OpenAI-compatible LLM and transcription providers.
- General-purpose application launching, window controls, file search, and file operations.
- Browser opening and web search.
- Text-to-speech with Edge neural voices and an offline system-voice fallback.
- Safety checks for desktop and file actions.

Application launching uses the installed-app discovery and alias mechanisms. Spotify, YouTube,
and the Windows Camera app depend on the app or browser being installed and available on the
computer. The project does not contain dedicated Spotify, YouTube, or camera integrations.

## Architecture

```text
"Hey Jarvis" -> openWakeWord -> microphone capture and VAD -> speech transcription
     -> Sienna agent and LLM -> safety policy -> desktop and browser tools
     -> spoken response
```

The wake-word model runs locally. Groq Whisper is the default transcription model, Groq is
the default LLM provider, and Edge neural TTS is the default speech output. OpenAI-compatible
provider settings are available as alternatives. Desktop operations are routed through
platform adapters and the general-purpose tool registry.

## Installation

### Windows setup

Install Python 3.10 or later, clone this repository, and run the setup script:

```bat
git clone https://github.com/sabby13/sos.git
cd sos
setup.bat
```

`setup.bat` creates `.venv`, installs the voice dependencies, creates `.env` from
`.env.example` if needed, and runs the setup diagnostics. Add a provider API key to `.env`
when prompted. For Groq, use `GROQ_API_KEY`; for OpenAI, use `OPENAI_API_KEY`.

Start voice mode with `jarvis.bat`. The launcher also forwards CLI commands, for example:

```bat
jarvis.bat doctor
jarvis.bat voice -k
jarvis.bat chat
jarvis.bat ask "open my downloads folder"
```

### Manual installation

From the repository directory, install the voice extras into a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[voice]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Add the provider key to `.env`, then run `jarvis.bat doctor` and `jarvis.bat` to start voice
mode. The voice extras include microphone capture, openWakeWord, and speech synthesis.

## Configuration

`jarvis config init` creates `~/.openjarvis/config.yaml`. Configuration precedence is
defaults, YAML, environment variables (including `.env`), then CLI overrides. API keys
belong in environment variables or `.env`, not in the YAML configuration.

For example:

```yaml
llm:
  provider: groq
  model: openai/gpt-oss-120b
  max_steps: 12
  max_tokens: 1024
  history_turns: 20

speech:
  wake_word: hey_jarvis
  wake_threshold: 0.5
  input_device: null
  stt_provider: groq
  tts_engine: edge
  tts_voice: en-US-AndrewNeural
  silence_ms: 900
  vad_sensitivity: 2.5
  followup_window_s: 8.0

tools:
  app_aliases:
    music: spotify
```

The existing wake-word setting accepts supported built-in model names or a path to a
compatible custom ONNX model. Setting it to `HelloSienna` alone does not create or train a
HelloSienna detector. Use `jarvis models` to list models available to the configured
provider, and `jarvis devices` to list microphone inputs.

## Example Voice Commands

Use the current activation phrase:

- "Hey Jarvis, open Spotify."
- "Hey Jarvis, play some music."
- "Hey Jarvis, open YouTube."
- "Hey Jarvis, open my camera."
- "Hey Jarvis, find my latest PDF."
- "Hey Jarvis, what do I have open?"

Commands depend on installed applications, provider availability, and the configured safety
policy. After a response, voice mode listens for follow-up commands for eight seconds without
requiring the wake phrase. Say "stop" to stop listening.

## Roadmap

Planned work, not current functionality:

- Train and evaluate a compatible **HelloSienna** wake-word model.
- Improve handling of provider rate limits and usage constraints.
- Add browser automation and screenshot/vision capabilities.
- Add interruptible speech output.
- Explore local LLM support and bring other platform adapters closer to Windows parity.

## License

MIT. See [LICENSE](LICENSE).
