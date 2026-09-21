const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const WebSocket = require('ws');

const isDev = process.env.NODE_ENV === 'development';

let mainWindow = null;
let floatingChatWindow = null;
let wsClient = null;

function getToken() {
  try {
    const tokenPath = path.join(__dirname, '..', '.run', 'engine.token');
    return fs.readFileSync(tokenPath, 'utf8').trim();
  } catch (err) {
    console.error('Failed to read token:', err);
    return null;
  }
}

function connectEngine() {
  const token = getToken();
  const wsUrl = token ? `ws://127.0.0.1:8000/ws/tasks?token=${token}` : 'ws://127.0.0.1:8000/ws/tasks';
  
  if (wsClient) {
    wsClient.close();
  }

  wsClient = new WebSocket(wsUrl);

  wsClient.on('open', () => {
    console.log('Main process connected to Engine WebSocket.');
    broadcastEngineMessage({ type: '_ws_status', status: 'connected' });
  });

  wsClient.on('message', (data) => {
    const msgStr = data.toString();
    broadcastEngineMessage(msgStr);
  });

  wsClient.on('close', () => {
    console.log('Engine WebSocket closed.');
    broadcastEngineMessage({ type: '_ws_status', status: 'disconnected' });
    setTimeout(connectEngine, 3000); // auto reconnect
  });
  
  wsClient.on('error', (err) => {
    console.error('WebSocket error:', err);
  });
}

function broadcastEngineMessage(msg) {
  const msgData = typeof msg === 'string' ? msg : JSON.stringify(msg);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('engine-message', msgData);
  }
  if (floatingChatWindow && !floatingChatWindow.isDestroyed()) {
    floatingChatWindow.webContents.send('engine-message', msgData);
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    titleBarStyle: 'hidden',
    transparent: true,
    vibrancy: 'ultra-dark', // macOS
    backgroundMaterial: 'acrylic', // Windows 11
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.cjs')
    }
  });

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173');
  } else {
    mainWindow.loadFile(path.join(__dirname, 'dist/index.html'));
  }
  
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function createFloatingChat(sessionId) {
  if (floatingChatWindow && !floatingChatWindow.isDestroyed()) {
    floatingChatWindow.focus();
    return;
  }

  floatingChatWindow = new BrowserWindow({
    width: 400,
    height: 600,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    hasShadow: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.cjs')
    }
  });

  if (isDev) {
    floatingChatWindow.loadURL(`http://localhost:5173?floating=true&session=${sessionId || ''}`);
  } else {
    floatingChatWindow.loadFile(path.join(__dirname, 'dist/index.html'), { query: { floating: 'true', session: sessionId || '' } });
  }

  floatingChatWindow.on('closed', () => {
    floatingChatWindow = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('chat-attached');
    }
  });
}

app.whenReady().then(() => {
  createWindow();
  connectEngine();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// IPC handlers
ipcMain.handle('close-window', (event) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  if (win) win.close()
});

ipcMain.handle('minimize-window', (event) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  if (win) win.minimize()
});

ipcMain.handle('select-directory', async (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  const result = await dialog.showOpenDialog(win, {
    properties: ['openDirectory', 'createDirectory'],
    title: 'Select Download Directory'
  });
  if (result.canceled) {
    return null;
  } else {
    return result.filePaths[0];
  }
});

ipcMain.handle('maximize-window', (event) => {
  const win = BrowserWindow.fromWebContents(event.sender)
  if (win) {
    if (win.isMaximized()) win.unmaximize()
    else win.maximize()
  }
});

ipcMain.handle('get-auth-token', () => {
  return getToken();
});

ipcMain.handle('get-ws-status', () => {
  return wsClient && wsClient.readyState === WebSocket.OPEN;
});

ipcMain.on('engine-send', (event, msg) => {
  if (wsClient && wsClient.readyState === WebSocket.OPEN) {
    wsClient.send(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
});

ipcMain.handle('open-floating-chat', (event, sessionId) => {
  createFloatingChat(sessionId);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('chat-detached');
  }
});

ipcMain.handle('close-floating-chat', () => {
  if (floatingChatWindow && !floatingChatWindow.isDestroyed()) {
    floatingChatWindow.close();
  }
});
