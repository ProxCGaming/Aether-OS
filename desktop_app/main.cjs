
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
const { app, BrowserWindow, ipcMain, dialog, Tray, Menu, Notification, globalShortcut, nativeImage, session } = require('electron');
const path = require('path');
const fs = require('fs');
const WebSocket = require('ws');
const { createWindowStateStore } = require('./electron/windowState.cjs');
const { createEngineLifecycle } = require('./electron/engineLifecycle.cjs');
const http = require('http');
const { spawn: spawnChildProcess } = require('child_process');
const { shouldNotify, notificationContent } = require('./electron/notify.cjs');
const { computeCaptureGeometry, selectScreenSource } = require('./electron/snipGeometry.cjs');
const { defaultSnipAccelerator } = require('./electron/snipShortcut.cjs');
const { createTransferStateStore } = require('./electron/transferState.cjs');
const {
  DEV_ORIGIN,
  isTrustedSender,
  getSenderUrl,
  validateEngineMessage,
  validateOpenPathArg,
  validateAccelerator,
  validateShowOpenDialogOptions,
  validateSessionId,
} = require('./electron/ipcValidation.cjs');
const {
  buildContentSecurityPolicy,
  withCspResponseHeaders,
  createPermissionRequestHandler,
  createPermissionCheckHandler,
  guardWillNavigate,
  windowOpenDecision,
  shouldGuardWebContents,
} = require('./electron/securityPolicy.cjs');

// S3-M7: the floating-window transfer state (full chat history +
// draft input) lives ONLY in this main-process store — never in
// renderer localStorage, where any renderer script could read it.
const floatingTransferState = createTransferStateStore();

const isDev = process.env.NODE_ENV === 'development';

// S3-M2: shell hardening — electron/securityPolicy.cjs owns every
// decision; the code below is only wiring. The trust options are
// the S3-M3 sender rule (dev origin, or dist/index.html in prod).
function securityPolicyOptions() {
  return {
    isDev,
    appIndexPath: path.join(__dirname, 'dist', 'index.html'),
  };
}

function installSecurityPolicy() {
  const options = securityPolicyOptions();
  // Permission handlers live on the default session: deny by
  // default, allow only the small justified set, and only from the
  // app's own page. The <webview> preview guests (M01) use their
  // own 'aether-preview' partition session and are untouched.
  session.defaultSession.setPermissionRequestHandler(
    createPermissionRequestHandler(options, (msg) => console.warn(msg))
  );
  session.defaultSession.setPermissionCheckHandler(
    createPermissionCheckHandler(options)
  );
  // CSP delivery: production ships the policy as a <meta> tag in
  // index.html (the built page loads from file://, where response
  // headers cannot deliver one). In dev, vite.config.js strips
  // that tag and this header serves the relaxed dev-only policy
  // (Vite HMR needs inline/eval) on the dev origin instead.
  if (isDev) {
    session.defaultSession.webRequest.onHeadersReceived(
      { urls: [`${DEV_ORIGIN}/*`], types: ['mainFrame'] },
      (details, callback) => {
        callback({
          responseHeaders: withCspResponseHeaders(
            details.responseHeaders,
            buildContentSecurityPolicy({ isDev: true })
          ),
        });
      }
    );
  }
}

// S3-M2: navigation + window-open guards for the app's OWN windows
// only. shouldGuardWebContents leaves <webview> preview guests
// alone (navigating them is the M01 preview feature) and skips
// devtools / background contents. A navigation away from the app's
// own page is cancelled inside guardWillNavigate; window.open is
// always denied — the renderer has no window.open call site, and
// opening local files already goes through the guarded
// 'open-path' IPC.
app.on('web-contents-created', (_event, contents) => {
  if (!shouldGuardWebContents(contents)) return;
  contents.on('will-navigate', (navEvent, url) => {
    if (!guardWillNavigate(navEvent, url, securityPolicyOptions())) {
      console.warn(`[security] Blocked navigation away from the app: ${url}`);
    }
  });
  contents.setWindowOpenHandler(() => windowOpenDecision());
});

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

// E10-F06: bundled-engine lifecycle (electron/engineLifecycle.cjs
// owns every decision; this is wiring). Lazily created so the dev
// flow only pays for what it uses.
let engineLifecycle = null;
function getEngineLifecycle() {
  if (!engineLifecycle) {
    engineLifecycle = createEngineLifecycle({
      spawn: spawnChildProcess,
      httpGet: (url, timeoutMs) => new Promise((resolve, reject) => {
        const req = http.get(url, { timeout: timeoutMs }, (res) => {
          res.resume();
          resolve(res.statusCode || 0);
        });
        req.on('timeout', () => req.destroy(new Error('engine health probe timed out')));
        req.on('error', reject);
      }),
      fs,
      platform: process.platform,
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      userDataPath: app.getPath('userData'),
      baseEnv: process.env,
      log: (...args) => console.log(...args),
    });
  }
  return engineLifecycle;
}

function getToken() {
  // E10-F06: when the shell spawned the bundled engine, read THAT
  // engine's token file (the engine writes .run/engine.token
  // relative to its own CWD, which the lifecycle set to the engine
  // run directory). The token still travels only as an Authorization
  // header from this process — see connectEngine below.
  if (engineLifecycle) {
    const spawnedTokenPath = engineLifecycle.tokenFilePath();
    if (spawnedTokenPath) {
      try {
        return fs.readFileSync(spawnedTokenPath, 'utf8').trim();
      } catch (err) {
        console.error('Failed to read spawned-engine token:', err);
      }
    }
  }
  try {
    // S3-N2 (contract note): this fallback assumes the engine was
    // started with the repo root as its CWD, so its CWD-relative
    // .run/engine.token (aether_common.auth.DEFAULT_TOKEN_PATH)
    // lands here. The spawned-engine path above is the production
    // route; moving the token location means moving the engine
    // default, the worker inheritance, the lifecycle path, and this
    // fallback together.
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
      // S3-M2: sandboxed renderer. preload.cjs requires only
      // 'electron' (contextBridge + ipcRenderer), which is exactly
      // the API set a sandboxed preload is allowed — pinned by
      // electron/__tests__/securityPolicy.test.js.
      sandbox: true,
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
      // S3-M2: sandboxed renderer, same as the main window.
      sandbox: true,
      // M01: same renderer bundle as the main window — see there.
      webviewTag: true,
      preload: path.join(__dirname, 'preload.cjs')
    }
  });

  if (isDev) {
    floatingChatWindow.loadURL(`http://localhost:5173?floating=true&session=${encodeURIComponent(sessionId || '')}`);
  } else {
    floatingChatWindow.loadFile(path.join(__dirname, 'dist/index.html'), { query: { floating: 'true', session: sessionId || '' } });
  }

  floatingChatWindow.on('closed', () => {
    floatingChatWindow = null;
    // S3-M7: the floating window has closed for real, so the
    // transfer state is cleared from main-process memory. The final
    // snapshot rides WITH the 'chat-attached' event (captured before
    // the clear) so the main window can still restore the floating
    // window's edits without the state persisting anywhere.
    const transferSnapshot = floatingTransferState.get();
    floatingTransferState.clear();
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('chat-attached', transferSnapshot);
    }
  });
}

// Epic 8, E8-4.1: global snip shortcut & capture manager
// Open Point 13 (DECIDED 1 Nov 2026): the snip hotkey is
// user-changeable in Settings; the default is Ctrl+S on
// Windows/Linux and Cmd+S on macOS (electron/snipShortcut.cjs
// owns the resolution). The stored preference lives in the
// engine user config (snip_hotkey); the renderer applies it at
// startup and on change through the register-snip-shortcut IPC.
let currentSnipShortcut = defaultSnipAccelerator(process.platform);
let snipShortcutActive = false;

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

// Failure rule (Open Point 13): a new accelerator is registered
// BEFORE the old one is dropped, so when registration fails —
// the accelerator is taken by the OS or another app, or invalid
// on this platform — the previous working hotkey stays active
// and currentSnipShortcut keeps pointing at it. Returns true
// only when `shortcut` is the live accelerator afterwards.
function registerSnipGlobalShortcut(shortcut) {
  const previous = currentSnipShortcut;
  const target = shortcut || previous;
  if (target === previous && snipShortcutActive) return true;
  currentSnipShortcut = target;
  let registered = false;
  try {
    registered = globalShortcut.register(currentSnipShortcut, triggerSnipFlow) !== false;
  } catch (err) {
    console.error('Failed to register snip shortcut:', err);
  }
  if (!registered) {
    currentSnipShortcut = previous;
    return false;
  }
  if (previous !== target) {
    try { globalShortcut.unregister(previous); } catch (e) { /* ignore */ }
  }
  snipShortcutActive = true;
  return true;
}

app.whenReady().then(() => {
  if (!gotSingleInstanceLock) return;
  installSecurityPolicy();
  createWindow();
  // E10-F06: if no engine already answers on the engine port, start
  // the bundled one first (packaged builds); connectEngine() itself
  // is unchanged and its reconnect loop covers every outcome,
  // including the dev case where no payload exists.
  void getEngineLifecycle().start()
    .catch((err) => console.error('Engine lifecycle start failed:', err))
    .finally(() => connectEngine());
  createTray();
  // Epic 6, E6-01: global summon hotkey (Alt+Space toggles the window).
  try {
    globalShortcut.register('Alt+Space', toggleMainWindow);
  } catch (err) {
    console.error('Failed to register summon hotkey:', err);
  }
  // Epic 8, E8-4.1: global screen snip hotkey (Settings-changeable;
  // Open Point 13 default Ctrl+S, Cmd+S on macOS). The renderer
  // re-applies the stored preference as soon as the config loads.
  registerSnipGlobalShortcut(currentSnipShortcut);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  // E10-F06: the spawned engine belongs to this app instance; it
  // must not outlive it. stop() issues the SIGTERM synchronously;
  // the process 'exit' killSync below is the backstop if the app
  // process dies before the graceful stop completes.
  if (engineLifecycle) void engineLifecycle.stop();
});

// E10-F06: last resort for abrupt exits, where no async quit hook
// runs — a synchronous kill attempt on the spawned engine.
process.on('exit', () => {
  if (engineLifecycle) engineLifecycle.killSync();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// IPC handlers
//
// S3-M3: trust boundary. Every handler below is a privileged
// action (engine relay, native dialogs, global shortcuts, shell
// open, screen capture). The sender is validated ONCE here, in
// this shared guard: the invoking frame's URL must be the app's
// own page — http://localhost:5173 in dev, dist/index.html as a
// file:// URL in production, for either the main window or the
// floating window (see electron/ipcValidation.cjs). A refusal is
// logged and the handler returns its shape's refusal value
// (null / false / error string) or throws where the handler's
// existing error shape is a throw. Renderer arguments for the
// privileged handlers are validated with the same module.
function isTrustedIpcSender(event, channel) {
  const trusted = isTrustedSender(event, {
    isDev,
    appIndexPath: path.join(__dirname, 'dist', 'index.html'),
  });
  if (!trusted) {
    console.warn(`[ipc] Refused '${channel}' from untrusted sender: ${getSenderUrl(event) || '(no URL)'}`);
  }
  return trusted;
}

ipcMain.handle('close-window', (event) => {
  if (!isTrustedIpcSender(event, 'close-window')) return;
  const win = BrowserWindow.fromWebContents(event.sender)
  if (win) win.close()
});

ipcMain.handle('minimize-window', (event) => {
  if (!isTrustedIpcSender(event, 'minimize-window')) return;
  const win = BrowserWindow.fromWebContents(event.sender)
  if (win) win.minimize()
});

ipcMain.handle('select-directory', async (event) => {
  if (!isTrustedIpcSender(event, 'select-directory')) return null;
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
  if (!isTrustedIpcSender(event, 'maximize-window')) return;
  const win = BrowserWindow.fromWebContents(event.sender)
  if (win) {
    if (win.isMaximized()) win.unmaximize()
    else win.maximize()
  }
});

ipcMain.handle('open-logs-folder', async (event) => {
  if (!isTrustedIpcSender(event, 'open-logs-folder')) {
    throw new Error('Refused to open the logs folder: untrusted sender.');
  }
  const path = require('path');
  const os = require('os');
  const fs = require('fs');
  const dir = path.join(os.homedir(), '.aether', 'logs');
  fs.mkdirSync(dir, { recursive: true });
  const { shell } = require('electron');
  await shell.openPath(dir);
  return dir;
});
ipcMain.handle('get-ws-status', (event) => {
  if (!isTrustedIpcSender(event, 'get-ws-status')) return false;
  return wsClient && wsClient.readyState === WebSocket.OPEN;
});

ipcMain.on('engine-send', (event, msg) => {
  if (!isTrustedIpcSender(event, 'engine-send')) return;
  const check = validateEngineMessage(msg);
  if (!check.ok) {
    console.warn(`[ipc] Refused engine-send message: ${check.error}`);
    return;
  }
  if (wsClient && wsClient.readyState === WebSocket.OPEN) {
    wsClient.send(check.value);
  }
});

ipcMain.handle('open-floating-chat', (event, sessionId) => {
  if (!isTrustedIpcSender(event, 'open-floating-chat')) return;
  const check = validateSessionId(sessionId);
  if (!check.ok) {
    console.warn(`[ipc] Refused open-floating-chat: ${check.error}`);
    return;
  }
  createFloatingChat(check.value);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('chat-detached');
  }
});

ipcMain.handle('close-floating-chat', (event) => {
  if (!isTrustedIpcSender(event, 'close-floating-chat')) return;
  if (floatingChatWindow && !floatingChatWindow.isDestroyed()) {
    floatingChatWindow.close();
  }
});

// S3-M7: floating-window transfer state — main-process memory only.
// The renderer sets it on detach, the floating window reads it on
// load (surviving its own renderer refresh) and writes its edits
// back; main clears it when the floating window closes (see the
// 'closed' handler in createFloatingChat). S3-M3: the transfer
// state is chat content, so these handlers sit behind the same
// sender guard as every other privileged handler.
ipcMain.handle('set-floating-transfer-state', (event, state) => {
  if (!isTrustedIpcSender(event, 'set-floating-transfer-state')) return false;
  return floatingTransferState.set(state);
});

ipcMain.handle('get-floating-transfer-state', (event) => {
  if (!isTrustedIpcSender(event, 'get-floating-transfer-state')) return null;
  return floatingTransferState.get();
});

ipcMain.handle('clear-floating-transfer-state', (event) => {
  if (!isTrustedIpcSender(event, 'clear-floating-transfer-state')) return false;
  floatingTransferState.clear();
  return true;
});

ipcMain.handle('get-memory-usage', (event) => {
  if (!isTrustedIpcSender(event, 'get-memory-usage')) {
    throw new Error('Refused memory usage request: untrusted sender.');
  }
  return Math.round(process.memoryUsage().rss / 1024 / 1024);
});

ipcMain.handle('show-open-dialog', async (event, options) => {
  if (!isTrustedIpcSender(event, 'show-open-dialog')) return null;
  const check = validateShowOpenDialogOptions(options);
  if (!check.ok) {
    console.warn(`[ipc] Refused show-open-dialog options: ${check.error}`);
    return null;
  }
  const win = BrowserWindow.fromWebContents(event.sender);
  const result = await dialog.showOpenDialog(win, check.value || {
    properties: ['openDirectory'],
    title: 'Select Folder'
  });
  if (result.canceled) return null;
  return result.filePaths[0];
});

// Epic 8, E8-4.1: Snip IPC handlers
ipcMain.handle('trigger-snip', (event) => {
  if (!isTrustedIpcSender(event, 'trigger-snip')) return false;
  triggerSnipFlow();
  return true;
});

ipcMain.handle('get-snip-shortcut', (event) => {
  if (!isTrustedIpcSender(event, 'get-snip-shortcut')) {
    throw new Error('Refused snip shortcut request: untrusted sender.');
  }
  return currentSnipShortcut;
});

ipcMain.handle('register-snip-shortcut', (event, shortcut) => {
  // Open Point 13: the reply reports the outcome honestly —
  // `ok` says whether the requested accelerator is now live,
  // and `shortcut` names the accelerator that IS live (the
  // previous one when a change failed), so Settings can show
  // the failure instead of saving a hotkey that does not work.
  if (!isTrustedIpcSender(event, 'register-snip-shortcut')) {
    return { ok: false, shortcut: currentSnipShortcut };
  }
  const check = validateAccelerator(shortcut);
  if (!check.ok) {
    console.warn(`[ipc] Refused snip shortcut: ${check.error}`);
    return { ok: false, shortcut: currentSnipShortcut };
  }
  const ok = registerSnipGlobalShortcut(check.value);
  if (!ok) {
    console.warn(`[ipc] Snip shortcut '${check.value}' could not be registered; '${currentSnipShortcut}' is still active.`);
  }
  return { ok, shortcut: currentSnipShortcut };
});

// N04: capture the display the overlay window is actually on, in
// that display's physical pixels. The rect arrives in window-client
// DIP coordinates; electron/snipGeometry.cjs owns the transform.
// Failure is honest: the handler throws, the renderer's invoke
// rejects, and the overlay tells the user the snip failed — there
// is no synthetic image and no fabricated success.
ipcMain.handle('capture-screen', async (event, rect) => {
  if (!isTrustedIpcSender(event, 'capture-screen')) {
    throw new Error('Screen capture failed: untrusted sender.');
  }
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
  if (!isTrustedIpcSender(event, 'open-path')) return 'Refused: untrusted sender.';
  const check = validateOpenPathArg(filePath);
  if (!check.ok) return check.error;
  const { shell } = require('electron');
  return shell.openPath(check.value);
});
