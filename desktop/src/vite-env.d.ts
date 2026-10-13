type DesktopRequest = {
  id: string
  type: 'ask' | 'voice' | 'get-settings' | 'configure' | 'confirmation'
  text?: string
  settings?: { ttsEngine?: 'edge' | 'system' | 'none' }
  confirmationId?: string
  approved?: boolean
}

type DesktopPreferences = {
  alwaysOnTop: boolean
  clickThrough: boolean
  startAtLogin: boolean
  ttsEngine: 'edge' | 'system' | 'none'
  windowBounds: { x: number; y: number; width: number; height: number } | null
}

declare global {
  type HelloSiennaEvent = {
    type: string
    requestId?: string
    state?: string
    text?: string
    message?: string
    hint?: string
    title?: string
    detail?: string
    confirmationId?: string
    name?: string
    preview?: string
    reason?: string
    ok?: boolean
    summary?: string
    settings?: {
      provider: string
      model: string
      apiKeyConfigured: boolean
      wakeWord: string
      ttsEngine: string
      ttsVoice: string
    }
  }

  interface Window {
    helloSienna: {
      send: (message: DesktopRequest) => void
      onEvent: (callback: (message: HelloSiennaEvent) => void) => () => void
      getPreferences: () => Promise<DesktopPreferences>
      setPreferences: (preferences: Partial<DesktopPreferences>) => Promise<DesktopPreferences>
      setExpanded: (expanded: boolean) => Promise<void>
      toggleWindow: () => Promise<void>
      closeApp: () => Promise<void>
      beginDrag: (screenX: number, screenY: number) => void
      dragTo: (screenX: number, screenY: number) => void
      endDrag: () => void
    }
  }
}

export {}
