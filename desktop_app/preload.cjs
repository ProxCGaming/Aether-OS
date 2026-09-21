const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  closeWindow: () => ipcRenderer.invoke('close-window'),
  minimizeWindow: () => ipcRenderer.invoke('minimize-window'),
  maximizeWindow: () => ipcRenderer.invoke('maximize-window'),
  selectDirectory: () => ipcRenderer.invoke('select-directory'),
  getAuthToken: () => ipcRenderer.invoke('get-auth-token'),
  getWsStatus: () => ipcRenderer.invoke('get-ws-status'),
  getMemoryUsage: () => ipcRenderer.invoke('get-memory-usage'),
  showOpenDialog: (options) => ipcRenderer.invoke('show-open-dialog', options),
  sendEngineMessage: (msg) => ipcRenderer.send('engine-send', msg),
  onEngineMessage: (callback) => {
    const handler = (_event, value) => callback(value);
    ipcRenderer.on('engine-message', handler);
    return () => ipcRenderer.removeListener('engine-message', handler);
  },
  onChatDetached: (callback) => {
    const handler = () => callback();
    ipcRenderer.on('chat-detached', handler);
    return () => ipcRenderer.removeListener('chat-detached', handler);
  },
  onChatAttached: (callback) => {
    const handler = () => callback();
    ipcRenderer.on('chat-attached', handler);
    return () => ipcRenderer.removeListener('chat-attached', handler);
  },
  openFloatingChat: (sessionId) => ipcRenderer.invoke('open-floating-chat', sessionId),
  closeFloatingChat: () => ipcRenderer.invoke('close-floating-chat')
});
