import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

type Message = {
  id: string
  text: string
  role: 'assistant' | 'user'
  time: string
}

type DesktopEvent = HelloSiennaEvent

type BackendSettings = {
  provider: string
  model: string
  apiKeyConfigured: boolean
  wakeWord: string
  ttsEngine: string
  ttsVoice: string
}

type Preferences = {
  alwaysOnTop: boolean
  clickThrough: boolean
  startAtLogin: boolean
  ttsEngine: 'edge' | 'system' | 'none'
  windowBounds: { x: number; y: number; width: number; height: number } | null
}

type PendingConfirmation = {
  id: string
  title: string
  detail: string
}

const defaultPreferences: Preferences = {
  alwaysOnTop: true,
  clickThrough: false,
  startAtLogin: false,
  ttsEngine: 'edge',
  windowBounds: null,
}

const activeStates = new Set(['listening', 'transcribing', 'processing', 'executing', 'speaking'])
const dotGlyphs: Record<string, string[]> = {
  H: ['10001', '10001', '10001', '11111', '10001', '10001', '10001'],
  E: ['11111', '10000', '10000', '11110', '10000', '10000', '11111'],
  L: ['10000', '10000', '10000', '10000', '10000', '10000', '11111'],
  O: ['01110', '10001', '10001', '10001', '10001', '10001', '01110'],
  S: ['01111', '10000', '10000', '01110', '00001', '00001', '11110'],
  I: ['11111', '00100', '00100', '00100', '00100', '00100', '11111'],
  N: ['10001', '11001', '11001', '10101', '10011', '10011', '10001'],
  A: ['01110', '10001', '10001', '11111', '10001', '10001', '10001'],
}

function DotMatrixWord() {
  const letters = 'HELLOSIENNA'.split('')
  const width = letters.length * 6 - 1
  return (
    <svg className="dot-word" viewBox={`0 0 ${width} 7`} role="img" aria-label="HelloSienna">
      {letters.flatMap((letter, letterIndex) =>
        dotGlyphs[letter].flatMap((row, rowIndex) =>
          [...row].flatMap((pixel, columnIndex) =>
            pixel === '1'
              ? [
                  <circle
                    key={`${letterIndex}-${rowIndex}-${columnIndex}`}
                    cx={letterIndex * 6 + columnIndex + 0.5}
                    cy={rowIndex + 0.5}
                    r="0.36"
                  />,
                ]
              : [],
          ),
        ),
      )}
    </svg>
  )
}

function timestamp() {
  return new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function App() {
  const [expanded, setExpanded] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [state, setState] = useState('idle')
  const [backendReady, setBackendReady] = useState(false)
  const [backendSettings, setBackendSettings] = useState<BackendSettings | null>(null)
  const [preferences, setPreferences] = useState<Preferences>(defaultPreferences)
  const [confirmation, setConfirmation] = useState<PendingConfirmation | null>(null)
  const [toolActivity, setToolActivity] = useState('')
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const suppressAvatarClickRef = useRef(false)

  const addMessage = useCallback((role: Message['role'], text: string, id: string = crypto.randomUUID()) => {
    setMessages((current) => {
      if (current.some((message) => message.id === id)) return current
      return [...current, { id, role, text, time: timestamp() }]
    })
  }, [])

  const syncPreferences = useCallback(async (patch?: Partial<Preferences>) => {
    const current = await window.helloSienna.setPreferences(patch ?? {})
    setPreferences(current)
    return current
  }, [])

  useEffect(() => {
    let mounted = true
    const unsubscribe = window.helloSienna.onEvent((event: DesktopEvent) => {
      if (!mounted) return
      switch (event.type) {
        case 'ready':
          setBackendReady(true)
          setState('idle')
          break
        case 'state':
          setState(event.state ?? 'idle')
          if (event.state === 'idle') setToolActivity('')
          break
        case 'transcript':
          if (event.text) addMessage('user', event.text, event.requestId)
          break
        case 'reply':
          if (event.text) addMessage('assistant', event.text, event.requestId ? `${event.requestId}-reply` : undefined)
          break
        case 'error':
          if (!event.requestId) setBackendReady(false)
          setState('error')
          if (event.message) {
            addMessage('assistant', `${event.message}${event.hint ? `\n${event.hint}` : ''}`, event.requestId ? `${event.requestId}-error` : undefined)
          }
          break
        case 'speech-error':
          if (event.message) setToolActivity(event.message)
          break
        case 'tool':
          setState('executing')
          setToolActivity(event.preview || event.name || 'Working with a tool')
          break
        case 'tool-result':
          setToolActivity(`${event.ok ? 'Completed' : 'Could not complete'}: ${event.summary ?? event.name ?? 'tool action'}`)
          break
        case 'tool-blocked':
          setToolActivity(`Blocked by safety policy: ${event.reason ?? event.name ?? 'action'}`)
          break
        case 'confirmation':
          if (event.confirmationId) {
            setConfirmation({
              id: event.confirmationId,
              title: event.title ?? 'Approve action',
              detail: event.detail ?? '',
            })
          }
          break
        case 'settings':
          setBackendReady(true)
          if (event.settings) setBackendSettings(event.settings)
          break
      }
    })

    void window.helloSienna.getPreferences().then((loaded) => {
      if (mounted) setPreferences(loaded)
    })
    window.helloSienna.send({ id: 'initial-settings', type: 'get-settings' })
    return () => {
      mounted = false
      unsubscribe()
    }
  }, [addMessage])

  useEffect(() => {
    if (expanded && !settingsOpen) inputRef.current?.focus()
  }, [expanded, settingsOpen])

  useEffect(() => {
    if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight
  }, [messages])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setConfirmation(null)
        setSettingsOpen(false)
        setExpanded(false)
        void window.helloSienna.setExpanded(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const changeExpanded = (value: boolean) => {
    setExpanded(value)
    setSettingsOpen(false)
    void window.helloSienna.setExpanded(value)
  }

  const handleSend = () => {
    const text = input.trim()
    if (!text || activeStates.has(state)) return
    const requestId = crypto.randomUUID()
    addMessage('user', text, requestId)
    setInput('')
    setState('processing')
    window.helloSienna.send({ id: requestId, type: 'ask', text })
  }

  const handleVoice = () => {
    if (activeStates.has(state)) return
    const requestId = crypto.randomUUID()
    setState('listening')
    setToolActivity('Listening through your selected microphone…')
    window.helloSienna.send({ id: requestId, type: 'voice' })
  }

  const handleConfirmation = (approved: boolean) => {
    if (!confirmation) return
    window.helloSienna.send({
      id: crypto.randomUUID(),
      type: 'confirmation',
      confirmationId: confirmation.id,
      approved,
    })
    setConfirmation(null)
  }

  const handleDragStart = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || (event.target instanceof Element && event.target.closest('button'))) return
    dragRef.current = { x: event.screenX, y: event.screenY, moved: false }
    event.currentTarget.setPointerCapture(event.pointerId)
    window.helloSienna.beginDrag(event.screenX, event.screenY)
  }

  const handleDragMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag) return
    if (Math.abs(event.screenX - drag.x) + Math.abs(event.screenY - drag.y) > 4) drag.moved = true
    if (drag.moved) window.helloSienna.dragTo(event.screenX, event.screenY)
  }

  const handleDragEnd = () => {
    if (!dragRef.current) return
    suppressAvatarClickRef.current = dragRef.current.moved
    window.helloSienna.endDrag()
    dragRef.current = null
    window.setTimeout(() => {
      suppressAvatarClickRef.current = false
    }, 0)
  }

  const handleAvatarClick = () => {
    if (suppressAvatarClickRef.current) return
    changeExpanded(!expanded)
  }

  const updatePreference = async (patch: Partial<Preferences>) => {
    await syncPreferences(patch)
  }

  const busy = activeStates.has(state)
  const statusLabel: Record<string, string> = {
    idle: backendReady ? 'ready' : 'connecting to OpenJarvis…',
    listening: 'listening',
    transcribing: 'transcribing',
    processing: 'thinking',
    executing: 'using a tool',
    speaking: 'speaking',
    error: 'backend error',
  }

  return (
    <main className="desktop-shell">
      <section className={`floating-widget ${expanded ? 'expanded' : ''}`} aria-label="HelloSienna assistant">
        <header
          className="bar-top"
          onPointerDown={handleDragStart}
          onPointerMove={handleDragMove}
          onPointerUp={handleDragEnd}
          onPointerCancel={handleDragEnd}
        >
          <button
            type="button"
            className={`avatar-wrap ${busy ? 'avatar-active' : ''}`}
            onClick={handleAvatarClick}
            aria-label={expanded ? 'Collapse HelloSienna' : 'Expand HelloSienna'}
          >
            <span className="avatar-circle">
              <span className="avatar-face" aria-hidden="true"><i /><i /><b /></span>
              {busy && <span className="avatar-pulse" />}
            </span>
          </button>
          <button
            className="brand-block"
            onClick={() => {
              if (!expanded) changeExpanded(true)
              setSettingsOpen(false)
            }}
            aria-label="Open HelloSienna chat"
            type="button"
          >
            <DotMatrixWord />
          </button>
          {expanded && (
            <div className="header-actions">
              <button
                type="button"
                className="icon-btn settings-trigger"
                onClick={() => setSettingsOpen((open) => !open)}
                aria-label={settingsOpen ? 'Close settings' : 'Open settings'}
                title="Settings"
              >
                ⚙
              </button>
              <button
                type="button"
                className="icon-btn"
                onClick={() => changeExpanded(false)}
                aria-label="Collapse"
                title="Collapse"
              >
                ×
              </button>
            </div>
          )}
        </header>

        {expanded && (
          <div className="expanded-content">
            {settingsOpen ? (
              <section className="settings-panel" aria-label="HelloSienna settings">
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">PREFERENCES</span>
                    <h1>Settings</h1>
                  </div>
                  <button className="text-button" type="button" onClick={() => setSettingsOpen(false)}>Done</button>
                </div>
                <div className="settings-group">
                  <label className="setting-row">
                    <span><strong>Keep on top</strong><small>Keep Sienna visible above other windows</small></span>
                    <input
                      type="checkbox"
                      checked={preferences.alwaysOnTop}
                      onChange={(event) => void updatePreference({ alwaysOnTop: event.target.checked })}
                    />
                  </label>
                  <label className="setting-row">
                    <span><strong>Click-through</strong><small>Let clicks pass through the avatar; Ctrl+Alt+H toggles it back</small></span>
                    <input
                      type="checkbox"
                      checked={preferences.clickThrough}
                      onChange={(event) => void updatePreference({ clickThrough: event.target.checked })}
                    />
                  </label>
                  <label className="setting-row">
                    <span><strong>Launch at sign-in</strong><small>Start HelloSienna when you sign in to Windows</small></span>
                    <input
                      type="checkbox"
                      checked={preferences.startAtLogin}
                      onChange={(event) => void updatePreference({ startAtLogin: event.target.checked })}
                    />
                  </label>
                  <label className="setting-row setting-select">
                    <span><strong>Speech output</strong><small>Edge Sonia neural voice, offline system voice, or silent</small></span>
                    <select
                      value={preferences.ttsEngine}
                      onChange={(event) => void updatePreference({ ttsEngine: event.target.value as Preferences['ttsEngine'] })}
                    >
                      <option value="edge">Sonia Neural (Edge)</option>
                      <option value="system">Windows system voice</option>
                      <option value="none">Off</option>
                    </select>
                  </label>
                </div>
                <div className="connection-card">
                  <span className={`connection-dot ${backendSettings?.apiKeyConfigured ? 'connected' : ''}`} />
                  <div>
                    <strong>{backendSettings ? `${backendSettings.provider} · ${backendSettings.model}` : 'Connecting to OpenJarvis'}</strong>
                    <small>
                      {backendSettings?.apiKeyConfigured
                        ? 'Model credentials are available from your local environment.'
                        : backendSettings
                          ? 'No provider key detected. Set the key in your local .env or environment.'
                          : 'The existing OpenJarvis agent, tools, and safety rules power this app.'}
                    </small>
                  </div>
                </div>
                <button className="text-button quit-button" type="button" onClick={() => window.helloSienna.closeApp()}>
                  Quit HelloSienna
                </button>
              </section>
            ) : (
              <>
                <div className="chat-header">
                  <span className={`state-indicator ${busy ? 'active' : ''}`} />
                  <span>{statusLabel[state] ?? state}</span>
                  <span className="chat-time">{timestamp()}</span>
                </div>
                <div className="chat-body" ref={messagesRef} aria-live="polite">
                  {messages.length === 0 && (
                    <div className="empty-state">
                      <span className="empty-orbit">✳</span>
                      <strong>Here when you need me.</strong>
                      <span>Ask OpenJarvis anything or use the microphone for a spoken request.</span>
                    </div>
                  )}
                  {messages.map((message) => (
                    <div key={message.id} className={`message-row ${message.role}`}>
                      <div className="message-bubble">{message.text}</div>
                      <span className="message-time">{message.time}</span>
                    </div>
                  ))}
                </div>
                {toolActivity && <div className="activity-line" title={toolActivity}>{toolActivity}</div>}
                <div className="composer">
                  <textarea
                    ref={inputRef}
                    value={input}
                    rows={1}
                    maxLength={8000}
                    onChange={(event) => setInput(event.target.value)}
                    placeholder="Ask Sienna anything…"
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        handleSend()
                      }
                    }}
                    aria-label="Message HelloSienna"
                  />
                  <button
                    type="button"
                    className={`mic-btn ${state === 'listening' ? 'recording' : ''}`}
                    onClick={handleVoice}
                    disabled={busy || !backendReady}
                    aria-label="Speak a message"
                    title="Record and send a voice request"
                  >
                    <span aria-hidden="true">{state === 'listening' ? '●' : '♩'}</span>
                  </button>
                  <button
                    type="button"
                    className="send-btn"
                    onClick={handleSend}
                    disabled={busy || !input.trim() || !backendReady}
                    aria-label="Send message"
                  >
                    ↑
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </section>

      {confirmation && (
        <div className="confirm-backdrop" role="presentation">
          <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
            <span className="eyebrow">SAFETY CHECK</span>
            <h2 id="confirm-title">{confirmation.title}</h2>
            <p>{confirmation.detail}</p>
            <div className="confirm-actions">
              <button type="button" className="text-button" onClick={() => handleConfirmation(false)}>Decline</button>
              <button type="button" className="approve-button" onClick={() => handleConfirmation(true)}>Approve</button>
            </div>
          </section>
        </div>
      )}
    </main>
  )
}

export default App
