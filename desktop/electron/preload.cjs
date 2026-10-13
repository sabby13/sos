const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('helloSienna', {
  send: (message) => ipcRenderer.send('backend:request', message),
  onEvent: (callback) => {
    const listener = (_event, message) => callback(message)
    ipcRenderer.on('backend:event', listener)
    return () => ipcRenderer.removeListener('backend:event', listener)
  },
  getPreferences: () => ipcRenderer.invoke('preferences:get'),
  setPreferences: (preferences) => ipcRenderer.invoke('preferences:set', preferences),
  getPillLayout: () => ipcRenderer.invoke('window:get-pill-layout'),
  setExpanded: (expanded) => ipcRenderer.invoke('window:set-expanded', expanded),
  setChatVisible: (visible) => ipcRenderer.invoke('window:set-chat-visible', visible),
  toggleWindow: () => ipcRenderer.invoke('window:toggle'),
  closeApp: () => ipcRenderer.invoke('app:quit'),
  beginDrag: (screenX, screenY) => ipcRenderer.send('window:drag-start', { screenX, screenY }),
  dragTo: (screenX, screenY) => ipcRenderer.send('window:drag-move', { screenX, screenY }),
  endDrag: () => ipcRenderer.send('window:drag-end'),
})
