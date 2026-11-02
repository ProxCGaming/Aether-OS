
// Epic 10: shell crash log — uncaught errors land next to the
// engine's own crash log in ~/.aether/logs for the diagnostics view.
try {
  const fs0 = require('fs');
  const path0 = require('path');
  const os0 = require('os');
  const crashDir = path0.join(os0.homedir(), '.aether', 'logs');
  fs0.mkdirSync(crashDir, { recursive: true });
  const crashPath = path0.join(crashDir, 'crash-electron.log');
  const writeCrash = (kind, err) => {
    try { fs0.appendFileSync(crashPath, `\n=== ${kind} ===\n${(err && err.stack) || err}\n`); } catch { /* never crash on logging */ }
  };
  process.on('uncaughtException', (err) => writeCrash('UNCAUGHT', err));
  process.on('unhandledRejection', (err) => writeCrash('UNHANDLED_REJECTION', err));
} catch { /* logging setup is best-effort */ }
const { app, BrowserWindow, ipcMain, dialog, Tray, Menu, Notification, globalShortcut, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const WebSocket = require('ws');
const { createWindowStateStore } = require('./electron/windowState.cjs');
const { shouldNotify, notificationContent } = require('./electron/notify.cjs');
const { computeCaptureGeometry, selectScreenSource } = require('./electron/snipGeometry.cjs');

const isDev = process.env.NODE_ENV === 'development';

let mainWindow = null;
let floatingChatWindow = null;
let wsClient = null;
let tray = null;
let windowStateStore = null;
let saveBoundsTimer = null;

// Epic 6, E6-01: single instance — a second launch focuses the
// running window instead of starting a duplicate app/engine link.
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
}
app.on('second-instance', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (!mainWindow.isVisible()) mainWindow.show();
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});

function getWindowStateStore() {
  if (!windowStateStore) {
    windowStateStore = createWindowStateStore({
      filePath: path.join(app.getPath('userData'), 'window-state.json'),
      fs,
    });
  }
  return windowStateStore;
}

function scheduleSaveBounds() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (saveBoundsTimer) clearTimeout(saveBoundsTimer);
  saveBoundsTimer = setTimeout(() => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      getWindowStateStore().save(mainWindow.getBounds());
    }
  }, 400);
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  if (!mainWindow.isVisible()) mainWindow.show();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
}

function toggleMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() && mainWindow.isFocused()) {
    mainWindow.hide();
  } else {
    showMainWindow();
  }
}

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
  // Phase 10.2 B1: the token is sent ONLY as an Authorization header.
  // It must never appear in the URL query string (URLs land in logs,
  // history and error reports). The token stays in the main process:
  // the renderer never holds it — main owns this socket and forwards
  // messages to the renderer via the 'engine-message' IPC channel.
  const token = getToken();
  const wsUrl = 'ws://127.0.0.1:8000/ws/tasks';

  if (wsClient) {
    wsClient.close();
  }

  wsClient = token
    ? new WebSocket(wsUrl, { headers: { Authorization: `Bearer ${token}` } })
    : new WebSocket(wsUrl);

  wsClient.on('open', () => {
    console.log('Main process connected to Engine WebSocket.');
    broadcastEngineMessage({ type: '_ws_status', status: 'connected' });
  });

  wsClient.on('message', (data) => {
    const msgStr = data.toString();
    broadcastEngineMessage(msgStr);
    // Epic 6, E6-01: OS notifications for task completion /
    // approval-needed, only when the user is not looking at the app
    // (decision logic in electron/notify.cjs, unit-tested).
    try {
      const visible = !!(mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible());
      const focused = !!(mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused());
      if (shouldNotify(msgStr, { windowVisible: visible, windowFocused: focused }) && Notification.isSupported()) {
        const content = notificationContent(msgStr);
        if (content) {
          const note = new Notification(content);
          note.on('click', showMainWindow);
          note.show();
        }
      }
    } catch {
      // Notifications are best-effort; never break message flow.
    }
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
  const bounds = getWindowStateStore().load();
  mainWindow = new BrowserWindow({
    width: bounds.width,
    height: bounds.height,
    ...(bounds.x !== undefined ? { x: bounds.x, y: bounds.y } : {}),
    titleBarStyle: 'hidden',
    transparent: true,
    vibrancy: 'ultra-dark', // macOS
    backgroundMaterial: 'acrylic', // Windows 11
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      // M01: ProjectPreview renders dev-server previews in a
      // <webview> guest (separate WebContents, own session
      // partition, no preload) so previewed content can never
      // reach this window's electronAPI bridge.
      webviewTag: true,
      preload: path.join(__dirname, 'preload.cjs')
    }
  });

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173');
  } else {
    mainWindow.loadFile(path.join(__dirname, 'dist/index.html'));
  }
  
  mainWindow.on('resize', scheduleSaveBounds);
  mainWindow.on('move', scheduleSaveBounds);
  mainWindow.on('close', () => {
    if (saveBoundsTimer) clearTimeout(saveBoundsTimer);
    getWindowStateStore().save(mainWindow.getBounds());
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function createTray() {
  if (tray) return;
  // Real tray icon (Epic 10): the packaged build carries
  // dist/tray-icon.png; dev falls back to public/ and finally to an
  // empty image so the menu + tooltip still work.
  const fs = require('fs');
  const candidates = [
    path.join(__dirname, 'dist', 'tray-icon.png'),
    path.join(__dirname, 'public', 'tray-icon.png'),
  ];
  const iconPath = candidates.find((p) => fs.existsSync(p));
  const image = iconPath
    ? nativeImage.createFromPath(iconPath).resize({ width: 32, height: 32 })
    : nativeImage.createEmpty();
  tray = new Tray(image);
  tray.setToolTip('Aether-OS');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show Aether-OS', click: showMainWindow },
    { label: 'Hide', click: () => { if (mainWindow && !mainWindow.isDestroyed()) mainWindow.hide(); } },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]));
  tray.on('click', toggleMainWindow);
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
      // M01: same renderer bundle as the main window — see there.
      webviewTag: true,
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

// Epic 8, E8-4.1: global snip shortcut & capture manager
let currentSnipShortcut = 'CommandOrControl+Shift+S';

function triggerSnipFlow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (!mainWindow.isVisible()) mainWindow.show();
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
    mainWindow.webContents.send('trigger-snip');
  }
  if (floatingChatWindow && !floatingChatWindow.isDestroyed()) {
    floatingChatWindow.webContents.send('trigger-snip');
  }
}

function registerSnipGlobalShortcut(shortcut) {
  if (currentSnipShortcut) {
    try { globalShortcut.unregister(currentSnipShortcut); } catch (e) { /* ignore */ }
  }
  currentSnipShortcut = shortcut || 'CommandOrControl+Shift+S';
  try {
    globalShortcut.register(currentSnipShortcut, triggerSnipFlow);
  } catch (err) {
    console.error('Failed to register snip shortcut:', err);
  }
}

app.whenReady().then(() => {
  if (!gotSingleInstanceLock) return;
  createWindow();
  connectEngine();
  createTray();
  // Epic 6, E6-01: global summon hotkey (Alt+Space toggles the window).
  try {
    globalShortcut.register('Alt+Space', toggleMainWindow);
  } catch (err) {
    console.error('Failed to register summon hotkey:', err);
  }
  // Epic 8, E8-4.1: global screen snip hotkey (Settings-changeable, default Ctrl+Shift+S / Cmd+Shift+S)
  registerSnipGlobalShortcut(currentSnipShortcut);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
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

ipcMain.handle('open-logs-folder', async () => {
  const path = require('path');
  const os = require('os');
  const fs = require('fs');
  const dir = path.join(os.homedir(), '.aether', 'logs');
  fs.mkdirSync(dir, { recursive: true });
  const { shell } = require('electron');
  await shell.openPath(dir);
  return dir;
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

ipcMain.handle('get-memory-usage', () => {
  return Math.round(process.memoryUsage().rss / 1024 / 1024);
});

ipcMain.handle('show-open-dialog', async (event, options) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  const result = await dialog.showOpenDialog(win, options || {
    properties: ['openDirectory'],
    title: 'Select Folder'
  });
  if (result.canceled) return null;
  return result.filePaths[0];
});

// Epic 8, E8-4.1: Snip IPC handlers
ipcMain.handle('trigger-snip', () => {
  triggerSnipFlow();
  return true;
});

ipcMain.handle('get-snip-shortcut', () => {
  return currentSnipShortcut;
});

ipcMain.handle('register-snip-shortcut', (event, shortcut) => {
  registerSnipGlobalShortcut(shortcut);
  return currentSnipShortcut;
});

// N04: capture the display the overlay window is actually on, in
// that display's physical pixels. The rect arrives in window-client
// DIP coordinates; electron/snipGeometry.cjs owns the transform.
// Failure is honest: the handler throws, the renderer's invoke
// rejects, and the overlay tells the user the snip failed — there
// is no synthetic image and no fabricated success.
ipcMain.handle('capture-screen', async (event, rect) => {
  const { desktopCapturer, screen } = require('electron');
  const win = BrowserWindow.fromWebContents(event.sender);
  if (!win || win.isDestroyed()) {
    throw new Error('Screen capture failed: the app window is not available.');
  }
  const primaryDisplay = screen.getPrimaryDisplay();
  const displays = [
    primaryDisplay,
    ...screen.getAllDisplays().filter((d) => d.id !== primaryDisplay.id),
  ];
  const geometry = computeCaptureGeometry({
    rect,
    windowContentBounds: win.getContentBounds(),
    displays,
  });
  if (!geometry) {
    throw new Error('Screen capture failed: the selected region is not on a connected display.');
  }
  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: geometry.thumbnailSize
  });
  const source = selectScreenSource(sources, geometry);
  if (!source || !source.thumbnail
      || (typeof source.thumbnail.isEmpty === 'function' && source.thumbnail.isEmpty())) {
    throw new Error('Screen capture failed: no screen image was returned for that display.');
  }
  try {
    return source.thumbnail.crop(geometry.crop).toDataURL();
  } catch (err) {
    throw new Error(`Screen capture failed: ${(err && err.message) || 'could not crop the selected region.'}`);
  }
});

// N06: open a file with the OS default application (the canvas
// "Open externally" action). Resolves to '' on success or the
// shell's error string — the renderer surfaces it, never a picker.
ipcMain.handle('open-path', async (event, filePath) => {
  if (!filePath || typeof filePath !== 'string') {
    return 'No file path was provided.';
  }
  const { shell } = require('electron');
  return shell.openPath(filePath);
});
