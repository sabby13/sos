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
stdin/stdout, while diagnostics remain on stderr.

The voice button is push-to-talk: click once, speak, and pause. Voice capture,
transcription, the model/tool loop, safety approval, and speech playback run in
the Python backend. The bridge intentionally serializes turns so the assistant
does not open duplicate microphone or playback sessions. Potentially unsafe
actions show the OpenJarvis safety preview and require explicit approval.

## Settings and controls

- Click the avatar or brand to expand/collapse the chat.
- Drag the avatar's outer edge or header to move the window; its position is persisted.
- The gear panel controls always-on-top, click-through, sign-in startup, and speech.
- `Ctrl+Alt+H` or the tray menu turns click-through off again if the window stops
  accepting mouse input.
- The tray menu can show the app, change click-through, restart the Python bridge,
  or quit. Closing the widget hides it in the tray.
- Speech output choices are Edge Sonia Neural, the Windows system voice, or off.
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
