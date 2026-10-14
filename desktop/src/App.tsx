import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

type Message = {
  id: string
  text: string
  role: 'assistant' | 'user'
  createdAt: number
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
  textProvider: 'ollama' | 'openjarvis'
  ollamaModel: string
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
  textProvider: 'ollama',
  ollamaModel: '',
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
function timestamp(value: number) {
  return new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function conversationTimestamp(value: number) {
  const date = new Date(value)
  const today = new Date()
  const sameDay = date.toDateString() === today.toDateString()
  const label = sameDay
    ? 'Today'
    : date.toLocaleDateString([], { month: 'short', day: 'numeric' })
  return `${label}, ${timestamp(value)}`
}

function App() {
  const [expanded, setExpanded] = useState(false)
  const [pillReady, setPillReady] = useState(false)
  const [pillDirection, setPillDirection] = useState<'left' | 'right'>('right')
  const [pillWidth, setPillWidth] = useState(PILL_WIDTH)
  const [chatOpen, setChatOpen] = useState(false)
  const [chatClosing, setChatClosing] = useState(false)
  const [pillMessage, setPillMessage] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [voicePending, setVoicePending] = useState(false)
  const [state, setState] = useState('idle')
  const [backendReady, setBackendReady] = useState(false)
  const [backendSettings, setBackendSettings] = useState<BackendSettings | null>(null)
  const [ollamaAvailable, setOllamaAvailable] = useState(false)
  const [ollamaModels, setOllamaModels] = useState<{ name: string; size: number }[]>([])
  const [preferences, setPreferences] = useState<Preferences>(defaultPreferences)
  const [confirmation, setConfirmation] = useState<PendingConfirmation | null>(null)
  const [toolActivity, setToolActivity] = useState('')
  const [avatarEdgeHovered, setAvatarEdgeHovered] = useState(false)
  const [avatarEdgeDragging, setAvatarEdgeDragging] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const messagesAtBottomRef = useRef(true)
  const dragRef = useRef<{ x: number; y: number; moved: boolean; source: 'header' | 'avatar-edge' } | null>(null)
  const suppressAvatarClickRef = useRef(0)
  const activeRequestIdRef = useRef<string | null>(null)
  const busy = activeStates.has(state) || voicePending
  const selectedOllamaModel = ollamaModels.some((model) => model.name === preferences.ollamaModel)
    ? preferences.ollamaModel
    : ollamaModels[0]?.name ?? preferences.ollamaModel

  const addMessage = useCallback((role: Message['role'], text: string, id: string = crypto.randomUUID()) => {
    setMessages((current) => {
      if (current.some((message) => message.id === id)) return current
      return [...current, { id, role, text, createdAt: Date.now() }]
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
        case 'chat-delta':
          if (event.requestId && event.text) {
            const messageId = `${event.requestId}-reply`
            const delta = event.text
            setMessages((current) => {
              const existing = current.find((message) => message.id === messageId)
              if (existing) {
                return current.map((message) =>
                  message.id === messageId ? { ...message, text: message.text + delta } : message,
                )
              }
              return [...current, {
                id: messageId,
                role: 'assistant',
                text: delta,
                createdAt: Date.now(),
              }]
            })
          }
          break
        case 'chat-complete':
          if (event.requestId && typeof event.text === 'string') {
            const messageId = `${event.requestId}-reply`
            const response = event.text
            setMessages((current) => {
              const existing = current.find((message) => message.id === messageId)
              if (existing) {
                return current.map((message) =>
                  message.id === messageId ? { ...message, text: response } : message,
                )
              }
              return [...current, {
                id: messageId,
                role: 'assistant',
                text: response,
                createdAt: Date.now(),
              }]
            })
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
        case 'ollama-status':
          setOllamaAvailable(Boolean(event.available))
          setOllamaModels(event.models ?? [])
          if (!activeRequestIdRef.current) setToolActivity(event.message ?? '')
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
      window.helloSienna.send({ id: 'initial-ollama-status', type: 'get-ollama' })
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
    const element = messagesRef.current
    if (element && messagesAtBottomRef.current) element.scrollTop = element.scrollHeight
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
    if (!visible && chatOpen) {
      setChatClosing(true)
      return
    }
    if (visible && !expanded) await changeExpanded(true)
    setChatClosing(false)
    setChatOpen(visible)
    if (!visible) setSettingsOpen(false)
    await window.helloSienna.setChatVisible(visible)
  }, [changeExpanded, chatOpen, expanded])

  const toggleChat = useCallback(async () => {
    await changeChatVisible(!chatOpen)
  }, [changeChatVisible, chatOpen])

  const handleSend = () => {
    const text = input.trim()
    if (!text || busy) return
    if (preferences.textProvider === 'ollama' && (!selectedOllamaModel || !ollamaAvailable)) {
      setToolActivity(
        ollamaAvailable
          ? 'Select an installed Ollama model in Settings before sending a local chat.'
          : 'Ollama is unavailable. Start Ollama and refresh its status in Settings.',
      )
      return
    }
    const requestId = crypto.randomUUID()
    addMessage('user', text, requestId)
    setInput('')
    setState('processing')
    activeRequestIdRef.current = requestId
    if (preferences.textProvider === 'ollama') {
      const context = [...messages.slice(-19), {
        id: requestId,
        role: 'user' as const,
        text,
        createdAt: Date.now(),
      }].map(({ role, text: content }) => ({ role, content }))
      window.helloSienna.send({
        id: requestId,
        type: 'ollama-chat',
        model: selectedOllamaModel,
        messages: context,
      })
      return
    }
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
    if (chatOpen) return
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

  const handleChatFoldEnd = (event: React.AnimationEvent<HTMLDivElement>) => {
    if (event.animationName !== 'chat-fold' || !chatClosing) return
    setChatOpen(false)
    setChatClosing(false)
    setSettingsOpen(false)
    void window.helloSienna.setChatVisible(false)
  }

  const updatePreference = async (patch: Partial<Preferences>) => {
    await syncPreferences(patch)
  }

  const refreshOllama = () => {
    window.helloSienna.send({ id: crypto.randomUUID(), type: 'get-ollama' })
  }

  const updateMessageScrollPosition = () => {
    const element = messagesRef.current
    if (element) {
      messagesAtBottomRef.current =
        element.scrollHeight - element.scrollTop - element.clientHeight < 80
    }
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
        className={`floating-widget${expanded ? ' expanded' : ''}${pillDirection === 'left' ? ' expands-left' : ''}${chatOpen ? ' chat-open' : ''}${chatClosing ? ' chat-closing' : ''}${expanded ? ` active-state-${state}` : ''}`}
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
          {chatOpen && (
            <button
              type="button"
              className="chat-menu-button"
              onClick={() => setSettingsOpen((open) => !open)}
              aria-label={settingsOpen ? 'Return to conversation' : 'Open settings'}
              title={settingsOpen ? 'Return to conversation' : 'Settings'}
            >
              <img src="./icons/menu.svg" alt="" aria-hidden="true" />
            </button>
          )}
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
              {!busy ? (
                <button
                  type="button"
                  className="pill-brand-button"
                  onClick={() => void changeChatVisible(true)}
                  aria-label="Open HelloSienna chat"
                  title="Open chat"
                >
                  <DotMatrixWord />
                </button>
              ) : (
                <DotMatrixWord />
              )}
              <button
                type="button"
                className={`pill-microphone${state === 'listening' ? ' recording' : ''}`}
                onClick={handleVoice}
                aria-label={state === 'listening' ? 'Microphone listening' : 'Start voice capture'}
                title="Start voice capture"
                disabled={busy || !backendReady}
                tabIndex={!busy ? 0 : -1}
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
              className={`close-cap${busy ? ' cancelling' : ''}${chatOpen ? ' chat-close' : ''}`}
              onClick={() => {
                if (busy) handleCancel()
                else if (chatOpen) void changeChatVisible(false)
                else void changeExpanded(false)
              }}
              aria-label={busy ? 'Cancel current request' : chatOpen ? 'Close chat' : 'Collapse HelloSienna'}
              title={busy ? 'Cancel request' : chatOpen ? 'Close chat' : 'Collapse'}
            >
              <img src="./icons/close.svg" alt="" aria-hidden="true" />
            </button>
          )}
        </header>

        {chatOpen && (
          <div className="expanded-content" onAnimationEnd={handleChatFoldEnd}>
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
                  <label className="setting-row setting-select">
                    <span>
                      <strong>Text-chat provider</strong>
                      <small>Local Ollama is the default. OpenJarvis uses the configured cloud agent and its safety checks.</small>
                    </span>
                    <select
                      value={preferences.textProvider}
                      onChange={(event) => void updatePreference({
                        textProvider: event.target.value as Preferences['textProvider'],
                      })}
                    >
                      <option value="ollama">Ollama (local)</option>
                      <option value="openjarvis">OpenJarvis agent</option>
                    </select>
                  </label>
                  <label className="setting-row setting-select">
                    <span><strong>Ollama model</strong><small>Used only for text chat when Ollama is selected.</small></span>
                    <select
                      value={selectedOllamaModel}
                      disabled={!ollamaAvailable || ollamaModels.length === 0}
                      onChange={(event) => void updatePreference({ ollamaModel: event.target.value })}
                    >
                      {ollamaModels.length === 0 && <option value="">No local models found</option>}
                      {ollamaModels.map((model) => (
                        <option key={model.name} value={model.name}>
                          {model.name} · {(model.size / 1_000_000_000).toFixed(1)} GB
                        </option>
                      ))}
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
                <div className="connection-card">
                  <span className={`connection-dot ${ollamaAvailable ? 'connected' : ''}`} />
                  <div>
                    <strong>{ollamaAvailable ? 'Ollama is running locally' : 'Ollama is unavailable'}</strong>
                    <small>
                      {ollamaAvailable
                        ? ollamaModels.length
                          ? `${ollamaModels.length} installed model(s). Local chat stays on this device.`
                          : 'Service is reachable, but no models are installed. Ollama does not download models automatically.'
                        : 'Connect only to 127.0.0.1:11434. Local chat will not fall back to a cloud provider.'}
                    </small>
                    <button className="text-button" type="button" onClick={refreshOllama}>
                      Refresh Ollama status
                    </button>
                  </div>
                </div>
                <button className="text-button quit-button" type="button" onClick={() => window.helloSienna.closeApp()}>
                  Quit HelloSienna
                </button>
              </section>
            ) : (
              <>
                <div
                  className="chat-body"
                  ref={messagesRef}
                  onScroll={updateMessageScrollPosition}
                  aria-live="polite"
                >
                  {messages.length === 0 && (
                    <div className="empty-state">
                      <strong>Here when you need me.</strong>
                      <span>
                        {preferences.textProvider === 'ollama'
                          ? 'Text stays on this device and uses your selected local Ollama model.'
                          : 'Messages use the OpenJarvis agent, including its configured provider and safety checks.'}
                      </span>
                    </div>
                  )}
                  {messages.map((message, index) => {
                    const previous = messages[index - 1]
                    const showDate = !previous
                      || new Date(previous.createdAt).toDateString()
                        !== new Date(message.createdAt).toDateString()
                    return (
                      <div key={message.id} className="message-group">
                        {showDate && (
                          <div className="chat-date">{conversationTimestamp(message.createdAt)}</div>
                        )}
                        <div className={`message-row ${message.role}`}>
                          <div className="message-bubble">{message.text}</div>
                          <span className="message-time">{timestamp(message.createdAt)}</span>
                        </div>
                      </div>
                    )
                  })}
                  {busy && state === 'processing' && (
                    <div className="chat-pending" role="status" aria-label="Sienna is thinking">
                      <i /><i /><i />
                    </div>
                  )}
                </div>
                {toolActivity && <div className="activity-line" title={toolActivity}>{toolActivity}</div>}
                <div className="composer">
                  <button
                    type="button"
                    className="attach-btn"
                    onClick={() => setToolActivity('Attachments are not supported by the current chat backend.')}
                    aria-label="Attachments unavailable"
                    title="Attachments are not supported"
                  >
                    <img src="./icons/add.svg" alt="" aria-hidden="true" />
                  </button>
                  <div className={`chat-input-pill${state === 'listening' ? ' listening' : ''}`}>
                    <textarea
                      ref={inputRef}
                      value={input}
                      rows={1}
                      maxLength={8000}
                      onChange={(event) => setInput(event.target.value)}
                      placeholder={
                        state === 'listening'
                          ? 'Listening…'
                          : state === 'transcribing'
                            ? 'Transcribing…'
                            : state === 'processing'
                              ? 'Thinking…'
                              : state === 'executing'
                                ? 'Working…'
                                : state === 'speaking'
                                  ? 'Speaking…'
                                  : 'type or speak'
                      }
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' && !event.shiftKey) {
                          event.preventDefault()
                          handleSend()
                        }
                      }}
                      aria-label="Message HelloSienna"
                      disabled={busy}
                    />
                    <button
                      type="button"
                      className="composer-action"
                      onClick={input.trim() ? handleSend : handleVoice}
                      disabled={busy || !backendReady}
                      aria-label={input.trim() ? 'Send message' : 'Start voice capture'}
                      title={input.trim() ? 'Send' : 'Start voice capture'}
                    >
                      <img
                        src={input.trim() ? './icons/arrow.svg' : './icons/mic.svg'}
                        alt=""
                        aria-hidden="true"
                      />
                    </button>
                  </div>
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
