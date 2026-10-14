const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, screen, globalShortcut } = require('electron')
const path = require('path')
const fs = require('fs')
const { spawn } = require('child_process')

const projectRoot = path.resolve(__dirname, '..', '..')
const AVATAR_SIZE = 98
const PILL_CAP_SIZE = 98
const PILL_WIDTH = 606
const CHAT_WIDTH = 520
const CHAT_HEIGHT = 600
const defaults = {
  alwaysOnTop: true,
  clickThrough: false,
  startAtLogin: false,
  ttsEngine: 'edge',
  textProvider: 'ollama',
  ollamaModel: '',
  windowBounds: null,
}

let mainWindow = null
let tray = null
let backendProcess = null
let backendBuffer = ''
let preferences = { ...defaults }
let dragStart = null
let savePositionTimer = null
let quitting = false
let windowExpanded = false
let windowDirection = 'right'
let chatVisible = false

function preferencesPath() {
  return path.join(app.getPath('userData'), 'desktop-settings.json')
}

function loadPreferences() {
  try {
    const saved = JSON.parse(fs.readFileSync(preferencesPath(), 'utf8'))
    preferences = { ...defaults, ...saved }
  } catch (error) {
    if (error.code !== 'ENOENT') console.error('[HelloSienna] Could not read settings:', error)
  }
}

function savePreferences() {
  try {
    fs.mkdirSync(app.getPath('userData'), { recursive: true })
    fs.writeFileSync(preferencesPath(), JSON.stringify(preferences, null, 2), 'utf8')
  } catch (error) {
    console.error('[HelloSienna] Could not save settings:', error)
  }
}

function emitToRenderer(message) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('backend:event', message)
  }
}

function pythonLaunch() {
  const bundled = path.join(process.resourcesPath, 'backend', 'openjarvis-backend.exe')
  if (app.isPackaged && fs.existsSync(bundled)) {
    return { executable: bundled, args: [] }
  }

  const configured = process.env.OPENJARVIS_PYTHON
  if (configured) {
    return { executable: configured, args: ['-m', 'openjarvis.desktop_bridge'] }
  }

  if (!app.isPackaged) {
    const localVenv = path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
    if (fs.existsSync(localVenv)) {
      return { executable: localVenv, args: ['-m', 'openjarvis.desktop_bridge'] }
    }
  }

  return process.platform === 'win32'
    ? { executable: 'py', args: ['-3', '-m', 'openjarvis.desktop_bridge'] }
    : { executable: 'python3', args: ['-m', 'openjarvis.desktop_bridge'] }
}

function startBackend() {
  if (backendProcess && backendProcess.exitCode === null && !backendProcess.killed) return

  const launch = pythonLaunch()
  backendBuffer = ''
  backendProcess = spawn(launch.executable, launch.args, {
    cwd: projectRoot,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  })
  backendProcess.stdout.setEncoding('utf8')
  backendProcess.stdout.on('data', (chunk) => {
    backendBuffer += chunk
    const lines = backendBuffer.split(/\r?\n/)
    backendBuffer = lines.pop() || ''
    for (const line of lines) {
      if (!line.trim()) continue
      try {
        emitToRenderer(JSON.parse(line))
      } catch (error) {
        console.error('[HelloSienna] Invalid backend event:', error, line)
      }
    }
  })
  backendProcess.stderr.setEncoding('utf8')
  backendProcess.stderr.on('data', (chunk) => console.error('[OpenJarvis]', chunk.trimEnd()))
  backendProcess.on('error', (error) => {
    emitToRenderer({
      type: 'error',
      message: `Could not start the OpenJarvis backend: ${error.message}`,
      hint: 'Install Python 3.10+ and the OpenJarvis dependencies. See desktop/README.md.',
    })
  })
  backendProcess.on('exit', (code, signal) => {
    if (!quitting) {
      emitToRenderer({
        type: 'error',
        message: `The OpenJarvis backend stopped (${signal || `exit code ${code}`}).`,
      })
    }
    backendProcess = null
  })
}

function sendBackendRequest(message) {
  if (!backendProcess || backendProcess.exitCode !== null || backendProcess.killed) {
    startBackend()
  }
  if (!backendProcess || !backendProcess.stdin.writable) {
    emitToRenderer({ type: 'error', requestId: message.id, message: 'OpenJarvis is not connected.' })
    return
  }
  backendProcess.stdin.write(`${JSON.stringify(message)}\n`, (error) => {
    if (error) {
      emitToRenderer({
        type: 'error',
        requestId: message.id,
        message: `Could not send the request to OpenJarvis: ${error.message}`,
      })
    }
  })
}

function usableBounds(bounds, minWidth = AVATAR_SIZE, minHeight = AVATAR_SIZE) {
  if (!bounds || !Number.isFinite(bounds.x) || !Number.isFinite(bounds.y)) return null
  const display = screen.getDisplayMatching(bounds)
  const area = display.workArea
  const width = Math.min(Math.max(minWidth, Math.round(bounds.width || AVATAR_SIZE)), area.width)
  const height = Math.min(Math.max(minHeight, Math.round(bounds.height || AVATAR_SIZE)), area.height)
  return {
    x: Math.min(Math.max(Math.round(bounds.x), area.x), area.x + area.width - width),
    y: Math.min(Math.max(Math.round(bounds.y), area.y), area.y + area.height - height),
    width,
    height,
  }
}

function pillBounds(anchorCenterX, y, width = PILL_WIDTH) {
  const display = screen.getDisplayMatching({
    x: anchorCenterX - AVATAR_SIZE / 2,
    y,
    width: AVATAR_SIZE,
    height: AVATAR_SIZE,
  })
  const area = display.workArea
  const targetWidth = Math.min(width, area.width)
  const extra = targetWidth - AVATAR_SIZE
  const visibleCenterX = Math.min(
    Math.max(anchorCenterX, area.x + AVATAR_SIZE / 2),
    area.x + area.width - AVATAR_SIZE / 2,
  )
  const rightX = visibleCenterX - AVATAR_SIZE / 2
  const leftX = rightX - extra
  if (rightX + targetWidth <= area.x + area.width) {
    return { x: rightX, y, width: targetWidth, direction: 'right' }
  }
  if (leftX >= area.x) {
    return { x: leftX, y, width: targetWidth, direction: 'left' }
  }
  return { x: rightX, y, width: targetWidth, direction: 'right' }
}

function avatarCenterX(bounds) {
  if (!windowExpanded) return bounds.x + AVATAR_SIZE / 2
  const capOffset = windowDirection === 'left' ? PILL_WIDTH - AVATAR_SIZE / 2 : AVATAR_SIZE / 2
  return bounds.x + capOffset
}

function persistWindowPosition() {
  if (!mainWindow || mainWindow.isDestroyed()) return
  clearTimeout(savePositionTimer)
  savePositionTimer = setTimeout(() => {
    const bounds = mainWindow.getBounds()
    const pillWidth = chatVisible ? PILL_WIDTH : bounds.width
    const centerX = windowExpanded
      ? bounds.x + (windowDirection === 'left' ? pillWidth - AVATAR_SIZE / 2 : AVATAR_SIZE / 2)
      : bounds.x + AVATAR_SIZE / 2
    preferences.windowBounds = {
      x: Math.round(centerX - AVATAR_SIZE / 2),
      y: Math.round(bounds.y),
      width: AVATAR_SIZE,
      height: AVATAR_SIZE,
    }
    savePreferences()
  }, 250)
}

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show()
    mainWindow.focus()
    return
  }

  const display = screen.getPrimaryDisplay()
  const saved = preferences.windowBounds
  const migratedPosition = saved && saved.width !== AVATAR_SIZE
    ? { x: saved.x + 141, y: saved.y + 17, width: AVATAR_SIZE, height: AVATAR_SIZE }
    : saved
  const initialBounds = (migratedPosition && usableBounds(migratedPosition)) || {
    x: Math.round(display.workArea.x + (display.workArea.width - AVATAR_SIZE) / 2),
    y: Math.round(display.workArea.y + (display.workArea.height - AVATAR_SIZE) / 2),
    width: AVATAR_SIZE,
    height: AVATAR_SIZE,
  }
  windowExpanded = false
  windowDirection = 'right'
  chatVisible = false
  mainWindow = new BrowserWindow({
    ...initialBounds,
    show: false,
    frame: false,
    transparent: true,
    alwaysOnTop: preferences.alwaysOnTop,
    focusable: true,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    skipTaskbar: true,
    hasShadow: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  if (preferences.alwaysOnTop) mainWindow.setAlwaysOnTop(true, 'screen-saver')
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
  mainWindow.setIgnoreMouseEvents(preferences.clickThrough, { forward: true })
  const devServerUrl = process.env.VITE_DEV_SERVER_URL || (!app.isPackaged ? 'http://127.0.0.1:5173' : '')
  if (devServerUrl) {
    mainWindow.loadURL(devServerUrl)
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }
  mainWindow.once('ready-to-show', () => mainWindow && mainWindow.show())
  mainWindow.on('move', persistWindowPosition)
  mainWindow.on('close', (event) => {
    if (!quitting) {
      event.preventDefault()
      mainWindow.hide()
    }
  })
  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

function toggleWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow()
  } else if (mainWindow.isVisible()) {
    mainWindow.hide()
  } else {
    mainWindow.show()
    mainWindow.focus()
  }
}

function makeTrayIcon() {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><circle cx="16" cy="16" r="14" fill="#080808" stroke="#f4d64e" stroke-width="2"/><circle cx="12" cy="13" r="1.5" fill="#f4d64e"/><circle cx="20" cy="13" r="1.5" fill="#f4d64e"/><path d="M11 20c2 3 8 3 10 0" fill="none" stroke="#f4d64e" stroke-width="1.5" stroke-linecap="round"/></svg>'
  return nativeImage.createFromDataURL(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`)
}

function updateTrayMenu() {
  if (!tray) return
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show HelloSienna', click: () => toggleWindow() },
    { label: 'Click-through (Ctrl+Alt+H)', type: 'checkbox', checked: preferences.clickThrough, click: (item) => setClickThrough(item.checked) },
    { type: 'separator' },
    { label: 'Restart OpenJarvis backend', click: () => restartBackend() },
    { label: 'Quit HelloSienna', click: () => app.quit() },
  ]))
}

function setClickThrough(enabled) {
  preferences.clickThrough = enabled
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.setIgnoreMouseEvents(enabled, { forward: true })
    if (!enabled) {
      mainWindow.show()
      mainWindow.focus()
    }
  }
  savePreferences()
  updateTrayMenu()
  return preferences
}

function restartBackend() {
  if (backendProcess) {
    backendProcess.once('exit', startBackend)
    backendProcess.stdin.end('{"type":"shutdown"}\n')
    setTimeout(() => {
      if (backendProcess) backendProcess.kill()
    }, 1000).unref()
  } else {
    startBackend()
  }
}

function setStartup(enabled) {
  preferences.startAtLogin = enabled
  app.setLoginItemSettings({ openAtLogin: enabled })
}

app.whenReady().then(() => {
  loadPreferences()
  startBackend()
  createWindow()
  tray = new Tray(makeTrayIcon())
  tray.setToolTip('HelloSienna')
  tray.on('click', toggleWindow)
  updateTrayMenu()
  globalShortcut.register('CommandOrControl+Alt+H', () => setClickThrough(!preferences.clickThrough))
  app.on('activate', createWindow)
})

app.on('browser-window-created', (_, window) => {
  window.setMenuBarVisibility(false)
  window.setAutoHideMenuBar(true)
})

ipcMain.on('backend:request', (event, message) => {
  if (event.sender !== mainWindow?.webContents || !message || typeof message !== 'object') return
  const allowed = new Set([
    'ask',
    'voice',
    'get-settings',
    'get-ollama',
    'ollama-chat',
    'configure',
    'confirmation',
    'cancel',
  ])
  if (!allowed.has(message.type)) return
  sendBackendRequest(message)
})

ipcMain.handle('preferences:get', () => preferences)

ipcMain.handle('preferences:set', (_, patch) => {
  if (!patch || typeof patch !== 'object') return preferences
  if (typeof patch.alwaysOnTop === 'boolean') {
    preferences.alwaysOnTop = patch.alwaysOnTop
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.setAlwaysOnTop(patch.alwaysOnTop, patch.alwaysOnTop ? 'screen-saver' : 'normal')
    }
  }
  if (typeof patch.clickThrough === 'boolean') setClickThrough(patch.clickThrough)
  if (typeof patch.startAtLogin === 'boolean') setStartup(patch.startAtLogin)
  if (['edge', 'system', 'none'].includes(patch.ttsEngine)) {
    preferences.ttsEngine = patch.ttsEngine
    sendBackendRequest({
      id: `configure-${Date.now()}`,
      type: 'configure',
      settings: {
        ttsEngine: patch.ttsEngine,
        ttsVoice: patch.ttsEngine === 'edge' ? 'en-GB-SoniaNeural' : null,
        ttsFallback: 'none',
      },
    })
  }
  if (['ollama', 'openjarvis'].includes(patch.textProvider)) {
    preferences.textProvider = patch.textProvider
  }
  if (typeof patch.ollamaModel === 'string' && patch.ollamaModel.length <= 200) {
    preferences.ollamaModel = patch.ollamaModel
  }
  savePreferences()
  updateTrayMenu()
  return preferences
})

ipcMain.handle('window:set-expanded', (_, expanded) => {
  if (!mainWindow || mainWindow.isDestroyed() || typeof expanded !== 'boolean') return null
  const current = mainWindow.getBounds()
  if (expanded) {
    chatVisible = false
    const layout = pillBounds(avatarCenterX(current), current.y)
    windowExpanded = true
    windowDirection = layout.direction
    mainWindow.setBounds({ x: layout.x, y: layout.y, width: layout.width, height: AVATAR_SIZE }, false)
    persistWindowPosition()
    return { direction: layout.direction, width: layout.width }
  }

  const centerX = avatarCenterX(current)
  const compact = usableBounds({
    x: centerX - AVATAR_SIZE / 2,
    y: current.y,
    width: AVATAR_SIZE,
    height: AVATAR_SIZE,
  })
  windowExpanded = false
  chatVisible = false
  windowDirection = 'right'
  mainWindow.setBounds(compact, false)
  persistWindowPosition()
  return { direction: 'right', width: AVATAR_SIZE }
})

ipcMain.handle('window:get-pill-layout', () => {
  if (!mainWindow || mainWindow.isDestroyed()) return { direction: 'right', width: PILL_WIDTH }
  const current = mainWindow.getBounds()
  const layout = pillBounds(avatarCenterX(current), current.y)
  return { direction: layout.direction, width: layout.width }
})

ipcMain.handle('window:set-chat-visible', (_, visible) => {
  if (!mainWindow || mainWindow.isDestroyed() || typeof visible !== 'boolean') return
  const current = mainWindow.getBounds()
  const centerX = avatarCenterX(current)
  const display = screen.getDisplayMatching({
    x: centerX - PILL_CAP_SIZE / 2,
    y: current.y,
    width: PILL_CAP_SIZE,
    height: AVATAR_SIZE,
  })
  const area = display.workArea
  const width = visible ? Math.min(Math.max(CHAT_WIDTH, PILL_WIDTH), area.width) : windowExpanded ? PILL_WIDTH : AVATAR_SIZE
  const height = visible ? Math.min(CHAT_HEIGHT, area.height) : AVATAR_SIZE
  const pillExtra = PILL_WIDTH - AVATAR_SIZE
  const x = Math.min(
    Math.max(centerX - AVATAR_SIZE / 2 - (windowDirection === 'left' ? pillExtra : 0), area.x),
    area.x + area.width - width,
  )
  const y = Math.min(Math.max(current.y, area.y), area.y + area.height - height)
  if (visible) windowExpanded = true
  chatVisible = visible
  mainWindow.setBounds({ x, y, width, height }, false)
  persistWindowPosition()
})

ipcMain.handle('window:toggle', toggleWindow)
ipcMain.handle('app:quit', () => app.quit())

ipcMain.on('window:drag-start', (event, point) => {
  if (event.sender !== mainWindow?.webContents || !Number.isFinite(point?.screenX) || !Number.isFinite(point?.screenY)) return
  const bounds = mainWindow.getBounds()
  dragStart = { offsetX: point.screenX - bounds.x, offsetY: point.screenY - bounds.y }
})

ipcMain.on('window:drag-move', (event, point) => {
  if (event.sender !== mainWindow?.webContents || !dragStart || !Number.isFinite(point?.screenX) || !Number.isFinite(point?.screenY)) return
  mainWindow.setPosition(Math.round(point.screenX - dragStart.offsetX), Math.round(point.screenY - dragStart.offsetY))
})

ipcMain.on('window:drag-end', (event) => {
  if (event.sender !== mainWindow?.webContents) return
  dragStart = null
  persistWindowPosition()
})

app.on('before-quit', () => {
  quitting = true
  clearTimeout(savePositionTimer)
  globalShortcut.unregisterAll()
  if (backendProcess && backendProcess.exitCode === null) {
    backendProcess.stdin.end('{"type":"shutdown"}\n')
    setTimeout(() => backendProcess && backendProcess.kill(), 1500).unref()
  }
  if (tray) tray.destroy()
})

app.on('window-all-closed', (event) => {
  event.preventDefault()
})
