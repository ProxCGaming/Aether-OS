const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  closeWindow: () => ipcRenderer.invoke('close-window'),
  minimizeWindow: () => ipcRenderer.invoke('minimize-window'),
  maximizeWindow: () => ipcRenderer.invoke('maximize-window'),
  selectDirectory: () => ipcRenderer.invoke('select-directory'),
  getAuthToken: () => ipcRenderer.invoke('get-auth-token'),
  getWsStatus: () => ipcRenderer.invoke('get-ws-status'),
  sendEngineMessage: (msg) => ipcRenderer.send('engine-send', msg),
  onEngineMessage: (callback) => ipcRenderer.on('engine-message', (_event, value) => callback(value)),
  onChatDetached: (callback) => ipcRenderer.on('chat-detached', () => callback()),
  onChatAttached: (callback) => ipcRenderer.on('chat-attached', () => callback()),
  openFloatingChat: (sessionId) => ipcRenderer.invoke('open-floating-chat', sessionId),
  closeFloatingChat: () => ipcRenderer.invoke('close-floating-chat')
});
