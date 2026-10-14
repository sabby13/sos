# HelloSienna desktop app

HelloSienna is an Electron floating companion for the existing OpenJarvis Python
agent. It does not duplicate the agent: text and voice requests go through the
same configured provider, tool registry, and safety policy as the OpenJarvis CLI.
The microphone button records a real utterance, transcribes it with the configured
Groq/OpenAI STT provider, runs the agent, and speaks the answer with the selected
TTS engine (Edge Sonia Neural by default).

## Windows development setup

From the repository root, install the Python project and its voice extras in a
Python 3.10+ environment. For example, in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[voice,dev]"


Set the provider key used by `config.yaml`/the OpenJarvis configuration in the
repository `.env` file or your user environment. Never put a key in frontend or
Electron source. The default provider is Groq; the existing `GROQ_API_KEY` and
`OPENAI_API_KEY` configuration is preserved. Install and run the desktop shell:

```powershell
cd desktop
npm install
npm run desktop:dev
```

`desktop:dev` starts Vite and Electron together. The Electron main process starts
one long-lived Python bridge; it prefers the repository `.venv` in development,
then the Python launcher (`py -3`). The bridge uses newline-delimited JSON over
stdin/stdout, while diagnostics remain on stderr. Bridge events are classified as
user/assistant messages, status, tool results, or errors; only conversational message
events enter chat history. Voice playback failures are reported as temporary status
and do not replace a completed command result or assistant response.

The voice button is push-to-talk: click once, speak, and pause. Voice capture,
transcription, the model/tool loop, safety approval, and speech playback run in
the Python backend. The bridge intentionally serializes turns so the assistant
does not open duplicate microphone or playback sessions. Potentially unsafe
actions show the OpenJarvis safety preview and require explicit approval.

## Settings and controls

- Click Sienna's portrait to unfold the horizontal voice pill. Click the supplied
  microphone icon to start voice capture. It moves to the pill center and pulses inside
  a yellow outline only while the backend reports that it is listening.
- Click the `helloSienna` LED label to unfold the conversation panel. The label and
  microphone are separate controls: the label opens chat and the microphone starts
  voice capture without changing modes. The chat menu opens settings; its X returns to
  the pill, and the pill X returns to the avatar.
- Typed chat uses the local Ollama API at `127.0.0.1:11434` by default. Voice requests
  continue through the configured OpenJarvis agent and speech pipeline. In Settings,
  choose **OpenJarvis agent** explicitly to route typed requests through the configured
  provider and its validated tools/safety checks. Local chat never silently falls back
  to a cloud provider and does not receive desktop tools.
- Install and start Ollama separately if it is not already available. If its local
  service is not running, start it with `ollama serve`. The app lists locally installed
  models and lets you choose one; it never downloads a model. To install one yourself,
  use `ollama pull <model>` after checking its download size and your machine's
  available memory. Ollama is accessed directly over loopback (not through configured
  HTTP proxies), and selected provider/model preferences are stored in the normal
  Electron settings file. Models need enough free system/GPU memory to run
  interactively; large models may be slow or fail to load on machines with limited
  resources.
- Conversation messages and local-model context stay in memory for the current app
  session; chat history is not persisted as personalized memory across restarts.
- Cancelling local generation is cooperative: Ollama has no separate interrupt
  endpoint, so the client can stop consuming once a streamed chunk arrives, but a
  stalled or already completed request may finish first.
- The chat attachment button currently reports that attachments are unsupported;
  selected files are not sent to either provider.
- Click the pill's X to collapse while idle, or to cooperatively cancel the current
  request while it is active. In-flight provider calls and desktop actions may finish
  before cancellation is observed; already completed actions cannot be undone.
- Drag the avatar's outer edge while compact, or the pill header while expanded, to
  move the native Electron window. Its avatar position is persisted across restarts.
- Right-click the pill label or press `Ctrl+Shift+C` to open the chat and message history.
  Press `Ctrl+,` to open settings; the gear panel controls always-on-top, click-through,
  sign-in startup, and speech. `Escape` cancels active work, then closes settings/chat,
  or collapses the idle pill.
- `Ctrl+Alt+H` or the tray menu turns click-through off again if the window stops
  accepting mouse input.
- The tray menu can show the app, change click-through, restart the Python bridge,
  or quit. Closing the widget hides it in the tray.
- Speech output choices are Microsoft Edge Sonia Neural
  (`en-GB-SoniaNeural`), the Windows system voice, or off. Selecting Sonia explicitly
  configures that voice for desktop replies, overriding a different OpenJarvis voice
  setting for the desktop session.
  STT and the LLM provider/model remain controlled by OpenJarvis configuration.

## Build and Windows packaging

```powershell
npm run build
npm run package:dir
npm run package:win
```

The production Electron installer currently packages the desktop shell, not a
Python runtime. On a target computer, install Python 3.10+ and the OpenJarvis
package with its `voice` extras and provider configuration as above. The packaged
app looks for `py -3` and an installed `openjarvis` package; set
`OPENJARVIS_PYTHON` to a specific interpreter when it is not discoverable through
the Python launcher. This is a documented external backend prerequisite, not a
standalone/offline installer.

The app stores window placement and desktop preferences under Electron's user
data directory. API keys remain in the established OpenJarvis environment/config
mechanism and are never returned to the renderer.
