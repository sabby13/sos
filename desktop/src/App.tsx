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

const activeStates = new Set(['listening', 'transcribing', 'processing', 'executing', 'speaking', 'cancelling'])
const PILL_WIDTH = 606

function DotMatrixWord() {
  return (
    <span className="hello-sienna-text" aria-label="HelloSienna">
      {'helloSienna'.split('').map((character, index) => (
        <span key={`${character}-${index}`} style={{ animationDelay: `${index * 38}ms` }}>{character}</span>
      ))}
    </span>
  )
}
function timestamp() {
  return new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function App() {
  const [expanded, setExpanded] = useState(false)
  const [pillReady, setPillReady] = useState(false)
  const [pillDirection, setPillDirection] = useState<'left' | 'right'>('right')
  const [pillWidth, setPillWidth] = useState(PILL_WIDTH)
  const [chatOpen, setChatOpen] = useState(false)
  const [pillMessage, setPillMessage] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [voicePending, setVoicePending] = useState(false)
  const [state, setState] = useState('idle')
  const [backendReady, setBackendReady] = useState(false)
  const [backendSettings, setBackendSettings] = useState<BackendSettings | null>(null)
  const [preferences, setPreferences] = useState<Preferences>(defaultPreferences)
  const [confirmation, setConfirmation] = useState<PendingConfirmation | null>(null)
  const [toolActivity, setToolActivity] = useState('')
  const [avatarEdgeHovered, setAvatarEdgeHovered] = useState(false)
  const [avatarEdgeDragging, setAvatarEdgeDragging] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{ x: number; y: number; moved: boolean; source: 'header' | 'avatar-edge' } | null>(null)
  const suppressAvatarClickRef = useRef(0)
  const activeRequestIdRef = useRef<string | null>(null)
  const busy = activeStates.has(state) || voicePending

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
          if (event.state) setVoicePending(false)
          if (event.state === 'idle') {
            setToolActivity('')
            setPillMessage('')
            activeRequestIdRef.current = null
          } else if (event.state === 'listening') {
            setPillMessage('')
          } else if (event.state === 'transcribing') {
            setPillMessage('Transcribing speech…')
          } else if (event.state === 'processing') {
            setPillMessage((current) => current || 'Working on it…')
          } else if (event.state === 'cancelling') {
            setPillMessage('Cancelling…')
          }
          break
        case 'transcript':
          if (event.text) {
            addMessage('user', event.text, event.requestId)
            setPillMessage(event.text)
          }
          break
        case 'reply':
          if (event.text) {
            addMessage('assistant', event.text, event.requestId ? `${event.requestId}-reply` : undefined)
            setPillMessage(event.text)
          }
          break
        case 'error':
          setVoicePending(false)
          if (!event.requestId) setBackendReady(false)
          setState('error')
          if (event.message) {
            setPillMessage(event.message)
            addMessage('assistant', `${event.message}${event.hint ? `\n${event.hint}` : ''}`, event.requestId ? `${event.requestId}-error` : undefined)
          }
          break
        case 'cancelled':
          activeRequestIdRef.current = null
          setVoicePending(false)
          setConfirmation(null)
          setToolActivity('')
          setPillMessage('')
          if (event.message) {
            addMessage('assistant', event.message, event.requestId ? `${event.requestId}-cancelled` : undefined)
          }
          break
        case 'cancel-error':
          setToolActivity(event.message ?? 'Could not cancel the active request.')
          setPillMessage(event.message ?? 'Could not cancel the active request.')
          break
        case 'speech-error':
          if (event.message) {
            setToolActivity(event.message)
            setPillMessage(`Speech playback failed: ${event.message}`)
            addMessage('assistant', `Speech playback failed: ${event.message}`, event.requestId ? `${event.requestId}-speech-error` : undefined)
          }
          break
        case 'tool':
          setState('executing')
          setToolActivity(event.preview || event.name || 'Working with a tool')
          setPillMessage(event.preview || event.name || 'Working with a tool')
          break
        case 'tool-result':
          setToolActivity(`${event.ok ? 'Completed' : 'Could not complete'}: ${event.summary ?? event.name ?? 'tool action'}`)
          setPillMessage(event.summary ?? event.name ?? 'Tool action complete')
          break
        case 'tool-blocked':
          setToolActivity(`Blocked by safety policy: ${event.reason ?? event.name ?? 'action'}`)
          setPillMessage('Action blocked by safety policy')
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
      if (!mounted) return
      setPreferences(loaded)
      window.helloSienna.send({
        id: 'initial-tts-settings',
        type: 'configure',
        settings: {
          ttsEngine: loaded.ttsEngine,
          ttsVoice: loaded.ttsEngine === 'edge' ? 'en-GB-SoniaNeural' : null,
          ttsFallback: 'none',
        },
      })
      window.helloSienna.send({ id: 'initial-settings', type: 'get-settings' })
    })
    return () => {
      mounted = false
      unsubscribe()
    }
  }, [addMessage])

  useEffect(() => {
    if (chatOpen && !settingsOpen) inputRef.current?.focus()
  }, [chatOpen, settingsOpen])

  useEffect(() => {
    if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight
  }, [messages])

  const changeExpanded = useCallback(async (value: boolean) => {
    setPillReady(false)
    setAvatarEdgeHovered(false)
    if (value) {
      const layout = await window.helloSienna.getPillLayout()
      if (layout) {
        setPillDirection(layout.direction)
        setPillWidth(layout.width)
      }
      setExpanded(true)
      await window.helloSienna.setExpanded(true)
      return
    }
    if (chatOpen) await window.helloSienna.setChatVisible(false)
    setExpanded(false)
    setChatOpen(false)
    setSettingsOpen(false)
  }, [chatOpen])

  const changeChatVisible = useCallback(async (visible: boolean) => {
    if (visible && !expanded) await changeExpanded(true)
    setChatOpen(visible)
    if (!visible) setSettingsOpen(false)
    await window.helloSienna.setChatVisible(visible)
  }, [changeExpanded, expanded])

  const toggleChat = useCallback(async () => {
    await changeChatVisible(!chatOpen)
  }, [changeChatVisible, chatOpen])

  const handleSend = () => {
    const text = input.trim()
    if (!text || activeStates.has(state)) return
    const requestId = crypto.randomUUID()
    addMessage('user', text, requestId)
    setInput('')
    setState('processing')
    activeRequestIdRef.current = requestId
    window.helloSienna.send({ id: requestId, type: 'ask', text })
  }

  const handleVoice = () => {
    if (activeStates.has(state) || voicePending || !backendReady) return
    const requestId = crypto.randomUUID()
    activeRequestIdRef.current = requestId
    setVoicePending(true)
    setToolActivity('Listening through your selected microphone…')
    window.helloSienna.send({ id: requestId, type: 'voice' })
  }

  const handleCancel = useCallback(() => {
    const requestId = activeRequestIdRef.current
    if (!requestId) return
    window.helloSienna.send({ id: crypto.randomUUID(), type: 'cancel', requestId })
  }, [])

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
    dragRef.current = { x: event.screenX, y: event.screenY, moved: false, source: 'header' }
    event.currentTarget.setPointerCapture(event.pointerId)
    window.helloSienna.beginDrag(event.screenX, event.screenY)
  }

  const handleDragEnd = () => {
    const drag = dragRef.current
    if (!drag) return
    if (drag.moved || drag.source === 'avatar-edge') {
      suppressAvatarClickRef.current = Date.now() + 350
    }
    if (drag.source === 'avatar-edge') setAvatarEdgeDragging(false)
    window.helloSienna.endDrag()
    dragRef.current = null
  }

  const handleDragMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag) return
    if ((event.buttons & 1) === 0) {
      handleDragEnd()
      return
    }
    if (Math.abs(event.screenX - drag.x) + Math.abs(event.screenY - drag.y) > 4) drag.moved = true
    if (drag.moved) window.helloSienna.dragTo(event.screenX, event.screenY)
  }

  const isNearAvatarEdge = (element: HTMLButtonElement, clientX: number, clientY: number) => {
    const bounds = element.getBoundingClientRect()
    const radius = Math.min(bounds.width, bounds.height) / 2
    const distance = Math.hypot(clientX - (bounds.left + bounds.width / 2), clientY - (bounds.top + bounds.height / 2))
    return Math.abs(distance - radius) <= 10
  }

  const handleAvatarPointerMove = (event: React.PointerEvent<HTMLButtonElement>) => {
    setAvatarEdgeHovered(!expanded && isNearAvatarEdge(event.currentTarget, event.clientX, event.clientY))
  }

  const handleAvatarPointerDown = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 || expanded || !isNearAvatarEdge(event.currentTarget, event.clientX, event.clientY)) return
    event.preventDefault()
    event.stopPropagation()
    dragRef.current = { x: event.screenX, y: event.screenY, moved: false, source: 'avatar-edge' }
    event.currentTarget.setPointerCapture(event.pointerId)
    setAvatarEdgeHovered(true)
    setAvatarEdgeDragging(true)
    window.helloSienna.beginDrag(event.screenX, event.screenY)
  }

  const handleAvatarClick = () => {
    if (Date.now() < suppressAvatarClickRef.current) return
    changeExpanded(!expanded)
  }

  const handlePillTransitionEnd = (event: React.TransitionEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget || event.propertyName !== 'width') return
    if (expanded) {
      setPillReady(true)
    } else {
      void window.helloSienna.setExpanded(false).then(() => setPillDirection('right'))
    }
  }

  const updatePreference = async (patch: Partial<Preferences>) => {
    await syncPreferences(patch)
  }

  const statusLabel: Record<string, string> = {
    idle: backendReady ? 'ready' : 'connecting to OpenJarvis…',
    listening: 'listening',
    transcribing: 'transcribing',
    processing: 'thinking',
    executing: 'using a tool',
    speaking: 'speaking',
    error: 'backend error',
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setConfirmation(null)
        if (busy) {
          handleCancel()
        } else if (settingsOpen) {
          setSettingsOpen(false)
        } else if (chatOpen) {
          void changeChatVisible(false)
        } else if (expanded) {
          changeExpanded(false)
        }
      } else if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === 'c') {
        event.preventDefault()
        void toggleChat()
      } else if (event.ctrlKey && event.key === ',') {
        event.preventDefault()
        void (async () => {
          if (!expanded) await changeExpanded(true)
          setChatOpen(true)
          setSettingsOpen(true)
          await window.helloSienna.setChatVisible(true)
        })()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [busy, chatOpen, changeChatVisible, changeExpanded, expanded, handleCancel, settingsOpen, toggleChat])

  return (
    <main className="desktop-shell">
      <section
        className={`floating-widget${expanded ? ' expanded' : ''}${pillDirection === 'left' ? ' expands-left' : ''}${chatOpen ? ' chat-open' : ''}${expanded ? ` active-state-${state}` : ''}`}
        style={{ '--pill-width': `${pillWidth}px` } as React.CSSProperties}
        aria-label="HelloSienna assistant"
        onTransitionEnd={handlePillTransitionEnd}
      >
        <header
          className="bar-top"
          onContextMenu={(event) => {
            event.preventDefault()
            void toggleChat()
          }}
          onPointerDown={handleDragStart}
          onPointerMove={handleDragMove}
          onPointerUp={handleDragEnd}
          onPointerCancel={handleDragEnd}
        >
          <button
            type="button"
            className={`avatar-wrap${avatarEdgeHovered ? ' edge-hovered' : ''}${avatarEdgeDragging ? ' edge-dragging' : ''}`}
            onPointerMove={handleAvatarPointerMove}
            onPointerDown={handleAvatarPointerDown}
            onPointerUp={handleDragEnd}
            onPointerCancel={handleDragEnd}
            onLostPointerCapture={handleDragEnd}
            onPointerLeave={() => setAvatarEdgeHovered(false)}
            onClick={handleAvatarClick}
            aria-label={expanded ? 'Collapse HelloSienna' : 'Expand HelloSienna'}
          >
            <img className="avatar-logo" src="./s_clear.png" alt="" aria-hidden="true" />
          </button>
          <div className="pill-connector" aria-hidden="true" />
          {expanded && pillReady && (
            <div className={`pill-center active-state-${state}`} aria-live="polite">
              <DotMatrixWord />
              <button
                type="button"
                className={`pill-microphone${state === 'listening' ? ' recording' : ''}`}
                onClick={handleVoice}
                aria-label={state === 'listening' ? 'Microphone listening' : 'Start voice capture'}
                title="Start voice capture"
                disabled={state !== 'idle' || voicePending || !backendReady}
                tabIndex={state === 'idle' ? 0 : -1}
              >
                <img src="./icons/mic.svg" alt="" aria-hidden="true" />
              </button>
              {state === 'transcribing' && (
                <span className="pill-activity processing-dots" role="status" aria-label="Transcribing">
                  <i /><i /><i />
                </span>
              )}
              {state === 'processing' && (
                <span className="pill-activity thinking-dots" role="status" aria-label="Thinking">
                  <i /><i /><i />
                </span>
              )}
              {['executing', 'speaking', 'cancelling', 'error'].includes(state) && (
                <span className={`pill-message state-${state}`} title={pillMessage}>
                  {pillMessage || statusLabel[state] || state}
                </span>
              )}
            </div>
          )}
          {expanded && (
            <button
              type="button"
              className={`close-cap${busy ? ' cancelling' : ''}`}
              onClick={() => busy ? handleCancel() : changeExpanded(false)}
              aria-label={busy ? 'Cancel current request' : 'Collapse HelloSienna'}
              title={busy ? 'Cancel request' : 'Collapse'}
            >
              <img src="./icons/close.svg" alt="" aria-hidden="true" />
            </button>
          )}
        </header>

        {chatOpen && (
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
                    <span><strong>Speech output</strong><small>Sonia neural voice (en-GB-SoniaNeural), Windows system voice, or silent</small></span>
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
                  <button
                    type="button"
                    className="icon-btn settings-trigger"
                    onClick={() => setSettingsOpen((open) => !open)}
                    aria-label="Open settings"
                    title="Settings"
                  >
                    ⚙
                  </button>
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
