<h1 align="center">OpenJarvis</h1>

<p align="center">
  <strong>An open-source, voice-first AI agent that actually operates your computer.</strong><br>
  Say "hey Jarvis" and talk to your desktop.
</p>

<p align="center">
  <a href="#install"><img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue"></a>
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
  <img alt="Windows" src="https://img.shields.io/badge/platform-Windows-informational">
</p>

---

OpenJarvis listens for a wake word, transcribes what you said, decides which tools to
run, and does it — then tells you what happened, out loud.

```
you    hey jarvis... find my latest PDF and open it
  -> find_files(extensions=['pdf'], newest_first=True)
  -> open_file(path='...\Downloads\invoice-april.pdf')
jarvis Found invoice-april.pdf from yesterday. Opening it now.
```

Things it handles today:

| You say | What happens |
|---|---|
| "Open Chrome" | Finds Chrome in the Start Menu and launches it |
| "Open VS Code and load my project" | Resolves the folder, launches the editor with it |
| "Find my latest PDF" | Searches your home folder, sorted by modification time |
| "Search my computer for Python files" | Whole-drive search (instant if you have Everything) |
| "Create a folder called Projects" | Creates it, in the right place |
| "Rename this file to notes.md" | Renames the file you were last working with |
| "What do I have open?" | Enumerates your windows |
| "Switch to Spotify" | Focuses the existing window instead of launching a second copy |
| "Delete that file" | Asks you first, then moves it to the Recycle Bin |

---

## How it works

```
                 "hey jarvis"
                      |
              [ wake word ]  openWakeWord, local, ~3% of one CPU core
                      |
              [ microphone ]  16 kHz capture + energy VAD (stops when you stop)
                      |
              [ transcribe ]  Groq whisper-large-v3-turbo  |  OpenAI whisper-1
                      |
              [   agent    ]  Groq gpt-oss-120b  |  OpenAI gpt-4o-mini
                      |            tool-calling loop, max 12 steps
                      |
              [ safety policy ]  safe -> run | caution -> run + log | dangerous -> ask
                      |
              [ tool router ]
                      |
   +---------+--------+--------+---------+
   |         |        |        |         |
  files   search    apps    windows   browser
   |         |        |        |         |
   +---------+--------+--------+---------+
                      |
              [   speak    ]  edge-tts neural voices (free) | pyttsx3 offline
```

**Every OS call sits behind a platform adapter.** Tools never touch `ctypes` or
`subprocess` themselves, so porting to macOS or Linux means implementing one file
(`platforms/base.py`) rather than auditing everything.

---

## Install

### Windows: double-click

```
git clone https://github.com/LiaqatEagle/OpenJarvis
cd OpenJarvis
```

Then **double-click `setup.bat`**. It creates the virtual environment, installs the voice
extras, writes your `.env`, and opens it so you can paste in an API key. Run it once.

After that, **double-click `jarvis.bat`** to start voice mode. That is the whole install.

`jarvis.bat` forwards its arguments, so every command below also works as
`jarvis.bat doctor`, `jarvis.bat chat`, and so on — no need to activate anything first.
Right-click it → *Send to* → *Desktop (create shortcut)* if you want it one click away.

### Any platform: manual

```bash
git clone https://github.com/LiaqatEagle/OpenJarvis
cd OpenJarvis

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[voice]"        # voice extras: wake word, mic, neural TTS
```

Then add an API key:

```bash
copy .env.example .env           # cp on macOS/Linux
```

```ini
GROQ_API_KEY=gsk_...
```

**Groq is the recommended starting point** — it has a free tier, and its
`whisper-large-v3-turbo` transcription typically returns in under a second, which is the
difference between an assistant that feels alive and one that feels broken. OpenAI works
identically; set `OPENAI_API_KEY` and pass `--provider openai`.

Check everything landed:

```bash
jarvis doctor
```

---

## Use

```bash
jarvis                          # voice mode - say "hey jarvis"
jarvis voice -k                 # push to talk: press Enter instead of a wake word
jarvis chat                     # text conversation, no microphone
jarvis ask "open my downloads folder"
```

Inspection and diagnostic commands:

```bash
jarvis doctor                   # check keys, mic, wake word, TTS - start here
jarvis mic                      # record 4s and show input levels in dBFS
jarvis tools                    # every tool, with its risk level
jarvis models                   # models your API key can actually reach
jarvis devices                  # list microphones
jarvis voices                   # list the free Edge neural voices
jarvis config init              # write ~/.openjarvis/config.yaml to edit
```

In voice mode, after each reply Jarvis keeps listening for **8 seconds without the wake
word**, so follow-ups work naturally. Say "stop" to put it back to sleep.

### If the wake word never fires

Nine times out of ten the selected input device is not your actual microphone — Windows
often defaults to "Microsoft Sound Mapper", which can be mapped to nothing. Run
`jarvis mic`. It prints a live level meter:

- **`-inf dB` / "no signal at all"** — the device is muted, disconnected, or blocked by
  Windows microphone privacy settings. Run `jarvis devices`, find your real mic, and set
  `speech.input_device` to its index.
- **peak around `-70 dB`** — the mic is live but heard no speech. Talk during the test.
- **peak above `-35 dB`** — healthy; speech will be detected.

### On model choice

The default on Groq is `openai/gpt-oss-120b` rather than the larger `llama-3.3-70b`,
because **tool-calling reliability matters more here than reasoning ability**. Llama
models on Groq intermittently emit their tool calls as plain text, which Groq's validator
rejects with `tool_use_failed` — an agent that can't reliably call a function is useless
no matter how clever it is. OpenJarvis retries those automatically, then tells you to
switch models if they persist.

---

## Safety

An LLM with shell access to your home directory is a genuinely bad idea unless something
independent of the model decides what it is allowed to do. In OpenJarvis that decision is
made by the safety policy, not by the prompt — a jailbroken model still cannot delete your
Documents folder without you pressing `y`.

Every tool declares a risk tier:

| Tier | Meaning | Default behaviour |
|---|---|---|
| `safe` | Observes only — list, search, read | Runs silently |
| `caution` | Reversible change — mkdir, rename, launch | Runs, printed to the console |
| `dangerous` | Destroys or executes — delete, overwrite | **Asks you first**, by voice or keyboard |

On top of that:

- **Deletes go to the Recycle Bin**, not to `unlink()`, unless you explicitly say permanent.
- **System paths are hard-blocked.** `C:\Windows`, `Program Files`, drive roots — refused
  before the tool ever runs, regardless of what the model asked for.
- **Optional allow-list.** Set `safety.allowed_roots` and file writes are confined to those
  folders and nothing else.
- **`--dry-run`** declines every confirmation, so you can watch a plan without it executing.

```yaml
# ~/.openjarvis/config.yaml
safety:
  mode: tiered              # tiered | strict (confirm everything) | auto (confirm nothing)
  allowed_roots:
    - ~/Documents
    - ~/Projects
  always_confirm: [launch_application]
  protect_system_paths: true
```

---

## Configuration

`jarvis config init` writes a commented file to `~/.openjarvis/config.yaml`. The layers,
lowest priority to highest: **defaults → config.yaml → environment / `.env` → CLI flags.**
API keys only ever come from the environment, so the YAML file is safe to commit or paste.

```yaml
llm:
  provider: groq              # groq | openai
  model: openai/gpt-oss-120b
  max_steps: 12               # ceiling on tool-call rounds per request

speech:
  wake_word: hey_jarvis       # or alexa, hey_mycroft, or a path to your own .onnx
  wake_threshold: 0.5         # raise if it fires at the TV, lower if it ignores you
  input_device: null          # index from `jarvis devices`; null = system default
  stt_provider: groq
  tts_engine: edge            # edge (free neural) | system (offline) | openai | groq
  tts_voice: en-US-AndrewNeural
  silence_ms: 900             # how long a pause means "I'm done talking"
  vad_sensitivity: 2.5        # lower if quiet speech gets cut off
  followup_window_s: 8.0

tools:
  app_aliases:
    music: spotify            # now "open music" launches Spotify
  search_roots:
    - D:/work
```

---

## Adding a tool

Tools are a pydantic model plus a `run` method. The docstrings you write are literally
what the model reads when deciding whether to call it.

```python
from pydantic import BaseModel, Field
from openjarvis.tools.base import RiskLevel, Tool, ToolContext, ToolResult


class SetVolumeArgs(BaseModel):
    level: int = Field(ge=0, le=100, description="Volume percentage.")


class SetVolume(Tool[SetVolumeArgs]):
    name = "set_volume"
    description = "Set the system output volume, 0 to 100."
    risk = RiskLevel.CAUTION
    args_model = SetVolumeArgs

    def run(self, args: SetVolumeArgs, ctx: ToolContext) -> ToolResult:
        ctx.platform.set_volume(args.level)
        return ToolResult.success(f"Volume set to {args.level} percent.")
```

Register it in `tools/registry.py` and it is live — schema generation, argument
validation, safety tiering, and error handling are all inherited.

Two things worth knowing:

- **Descriptions are prompt engineering.** Say when to prefer this tool over a similar
  one. Most "the model called the wrong tool" bugs are description bugs.
- **Return errors, don't raise them,** where the model could recover. `ToolResult.failure`
  goes back into the conversation so it can try something else; an exception ends the turn.

---

## Roadmap

- [ ] Terminal + git tools (shell execution behind the `dangerous` tier)
- [ ] Playwright browser automation — click, fill, extract
- [ ] Screenshot + vision, so "what's on my screen" works
- [ ] Barge-in: interrupt Jarvis mid-sentence
- [ ] Local models via Ollama, for a fully offline assistant
- [ ] macOS and Linux adapters brought up to parity with Windows

---

## Contributing

Issues and PRs welcome. The interfaces worth reading first:

| File | What it defines |
|---|---|
| `tools/base.py` | The tool contract |
| `platforms/base.py` | Everything an OS adapter must implement |
| `safety/policy.py` | How risk tiers become decisions |
| `agent/loop.py` | The tool-calling loop |
| `llm/base.py` | The provider interface |

```bash
pip install -e ".[dev,voice]"
pytest
ruff check src tests
```

---

## License

MIT. See [LICENSE](LICENSE).
