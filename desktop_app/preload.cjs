const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  closeWindow: () => ipcRenderer.invoke('close-window'),
  minimizeWindow: () => ipcRenderer.invoke('minimize-window'),
  maximizeWindow: () => ipcRenderer.invoke('maximize-window'),
  selectDirectory: () => ipcRenderer.invoke('select-directory'),
  getWsStatus: () => ipcRenderer.invoke('get-ws-status'),
  // Wave 1 slice 4: startup-gate surface. getEngineState resolves to
  // { state: 'starting' | 'started' | 'failed', logFilePath } — the
  // lifecycle STARTUP outcome from main (the socket status above
  // remains the readiness signal). onEngineState pushes the same
  // shape on every change.
  getEngineState: () => ipcRenderer.invoke('get-engine-state'),
  onEngineState: (callback) => {
    const handler = (_event, value) => callback(value);
    ipcRenderer.on('engine-state', handler);
    return () => ipcRenderer.removeListener('engine-state', handler);
  },
  retryEngineStart: () => ipcRenderer.invoke('retry-engine-start'),
  openEngineLog: () => ipcRenderer.invoke('open-engine-log'),
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
    // S3-M7: main sends the final transfer snapshot with this event
    // (its store is cleared at floating-window close), so the
    // snapshot is passed through to the callback.
    const handler = (_event, state) => callback(state);
    ipcRenderer.on('chat-attached', handler);
    return () => ipcRenderer.removeListener('chat-attached', handler);
  },
  setFloatingTransferState: (state) => ipcRenderer.invoke('set-floating-transfer-state', state),
  getFloatingTransferState: () => ipcRenderer.invoke('get-floating-transfer-state'),
  clearFloatingTransferState: () => ipcRenderer.invoke('clear-floating-transfer-state'),
  openFloatingChat: (sessionId) => ipcRenderer.invoke('open-floating-chat', sessionId),
  openLogsFolder: () => ipcRenderer.invoke('open-logs-folder'),
  closeFloatingChat: () => ipcRenderer.invoke('close-floating-chat'),
  triggerSnip: () => ipcRenderer.invoke('trigger-snip'),
  onTriggerSnip: (callback) => {
    const handler = () => callback();
    ipcRenderer.on('trigger-snip', handler);
    return () => ipcRenderer.removeListener('trigger-snip', handler);
  },
  captureScreen: (rect) => ipcRenderer.invoke('capture-screen', rect),
  openPath: (filePath) => ipcRenderer.invoke('open-path', filePath),
  registerSnipShortcut: (shortcut) => ipcRenderer.invoke('register-snip-shortcut', shortcut),
  getSnipShortcut: () => ipcRenderer.invoke('get-snip-shortcut')
});
