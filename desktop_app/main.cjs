
// Epic 10: shell crash log — uncaught errors land next to the
// engine's own crash log in ~/.aether/logs for the diagnostics view.
// Wave 3 (D4-01): the writer lives in electron/crashLog.cjs and
// ROTATES (5 MB, 3 archives) — the inline appendFileSync that used
// to live here grew the file without bound on a crash-looping
// install. It still never throws.
try {
  const fs0 = require('fs');
  const path0 = require('path');
  const os0 = require('os');
  const { createCrashLogWriter } = require('./electron/crashLog.cjs');
  const writeCrash = createCrashLogWriter({
    fs: fs0,
    crashDir: path0.join(os0.homedir(), '.aether', 'logs'),
  });
  process.on('uncaughtException', (err) => writeCrash('UNCAUGHT', err));
  process.on('unhandledRejection', (err) => writeCrash('UNHANDLED_REJECTION', err));
} catch { /* logging setup is best-effort */ }
const { app, BrowserWindow, ipcMain, dialog, Tray, Menu, Notification, globalShortcut, nativeImage, session, screen } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const WebSocket = require('ws');
const { createWindowStateStore, ensureVisibleOnDisplays } = require('./electron/windowState.cjs');
const { createEngineLifecycle } = require('./electron/engineLifecycle.cjs');
const { createReconnectGuard } = require('./electron/wsReconnect.cjs');
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
  validateUiLogEntry,
} = require('./electron/ipcValidation.cjs');
const {
  createAppLog,
  scrubSecrets,
  readLogTail,
  logFileStat,
} = require('./electron/appLog.cjs');
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

// Unified logging (5 Nov 2026): the shell's durable log. Every
// [main] line below goes through mainLog/mainWarn/mainError, which
// keep the existing console passthrough AND append to
// <userData>/logs/app.log via electron/appLog.cjs; renderer [ui]
// lines arrive through the append-ui-log IPC and land in the same
// file. Lazily created like the engine lifecycle, and the logger
// itself can never throw (see appLog.cjs), so a logging failure
// can never take the app down.
let appLog = null;
function getAppLog() {
  if (!appLog) {
    appLog = createAppLog({
      fs,
      userDataPath: app.getPath('userData'),
    });
  }
  return appLog;
}

function formatLogArgs(args) {
  return args
    .map((arg) => {
      if (arg instanceof Error) return arg.stack || arg.message || String(arg);
      if (typeof arg === 'string') return arg;
      try { return JSON.stringify(arg); } catch { return String(arg); }
    })
    .join(' ');
}

function mainLog(...args) {
  console.log(...args);
  try { getAppLog().info('main', formatLogArgs(args)); } catch { /* logging never breaks the app */ }
}

function mainWarn(...args) {
  console.warn(...args);
  try { getAppLog().warn('main', formatLogArgs(args)); } catch { /* logging never breaks the app */ }
}

function mainError(...args) {
  console.error(...args);
  try { getAppLog().error('main', formatLogArgs(args)); } catch { /* logging never breaks the app */ }
}

// The paths the log IPC reads. app.log + engine.log live in
// <userData>/logs (the lifecycle's file); crash-electron.log is
// the shell crash log from the block at the top of this file,
// next to the engine's own logs in ~/.aether/logs.
function appLogFilePath() {
  return getAppLog().filePath();
}

function engineLogFilePath() {
  return path.join(app.getPath('userData'), 'logs', 'engine.log');
}

function crashElectronLogPath() {
  return path.join(os.homedir(), '.aether', 'logs', 'crash-electron.log');
}

function logInventory() {
  return [
    logFileStat(fs, appLogFilePath(), 'app.log'),
    logFileStat(fs, engineLogFilePath(), 'engine.log'),
    logFileStat(fs, crashElectronLogPath(), 'crash-electron.log'),
  ].filter(Boolean);
}

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
    createPermissionRequestHandler(options, (msg) => mainWarn(msg))
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
      mainWarn(`[security] Blocked navigation away from the app: ${url}`);
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
      log: (...args) => mainLog(...args),
    });
  }
  return engineLifecycle;
}

// Wave 1 slice 4: the renderer's startup gate needs to know when the
// bundled engine has DEFINITIVELY failed to start — the lifecycle's
// terminal mode 'none', reached only after its initial-spawn retry —
// as opposed to "still starting", which can legitimately take the
// full 120s health budget. The lifecycle exposes no events, so main
// (its owner) tracks the startup outcome here: 'starting' until
// start() settles, then 'started' (spawned/external) or 'failed'
// ('none'). This state is about STARTUP only: mid-session lifecycle
// outcomes keep flowing through the socket status as before. The
// socket connecting remains the renderer's real readiness signal;
// this channel only ever adds the failure fact.
let engineStartupState = { state: 'starting', logFilePath: null };

function setEngineStartupState(state) {
  const logFilePath = engineLifecycle ? engineLifecycle.logFilePath() : engineStartupState.logFilePath;
  engineStartupState = { state, logFilePath };
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('engine-state', engineStartupState);
  }
}

// Drives one lifecycle start attempt and mirrors its outcome into
// engineStartupState. Used at app-ready and by the gate's Retry.
function startEngineAndTrack() {
  setEngineStartupState('starting');
  return getEngineLifecycle().start()
    .then((result) => {
      setEngineStartupState(result && result.mode === 'none' ? 'failed' : 'started');
      return result;
    })
    .catch((err) => {
      mainError('Engine lifecycle start failed:', err);
      setEngineStartupState('failed');
      return null;
    });
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
        mainError('Failed to read spawned-engine token:', err);
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
    mainError('Failed to read token:', err);
    return null;
  }
}

// D4-09: reconnect scheduling is generation-guarded
// (electron/wsReconnect.cjs). The old shape — a bare
// setTimeout(connectEngine, 3000) in the close handler — never
// stored or cleared the handle, so a superseded socket's close
// could schedule a competing reconnect and a pending timer could
// fire after quit had begun.
const reconnectGuard = createReconnectGuard({ reconnect: () => connectEngine() });
let reconnectGeneration = 0;

function scheduleEngineReconnect() {
  reconnectGuard.schedule(reconnectGeneration);
}

function connectEngine() {
  // Phase 10.2 B1: the token is sent ONLY as an Authorization header.
  // It must never appear in the URL query string (URLs land in logs,
  // history and error reports). The token stays in the main process:
  // the renderer never holds it — main owns this socket and forwards
  // messages to the renderer via the 'engine-message' IPC channel.
  const token = getToken();
  const wsUrl = 'ws://127.0.0.1:8000/ws/tasks';

  // Beginning a (re)connect supersedes every earlier socket and
  // clears any pending reconnect timer.
  reconnectGeneration = reconnectGuard.nextGeneration();

  if (wsClient) {
    wsClient.close();
  }

  if (!token) {
    // D4-09: the engine REQUIRES registration of its token —
    // a legacy engine without one refuses tokenless sockets with
    // 1008, so opening one only bought a guaranteed reject + close
    // + retry cycle. Do not open a socket we know will be refused;
    // wait for the token (it lands when the engine finishes
    // starting) and retry on the guard's schedule.
    wsClient = null;
    mainWarn('No engine token is available yet; will retry.');
    scheduleEngineReconnect();
    return;
  }

  const socket = new WebSocket(wsUrl, { headers: { Authorization: `Bearer ${token}` } });
  wsClient = socket;

  socket.on('open', () => {
    mainLog('Main process connected to Engine WebSocket.');
    broadcastEngineMessage({ type: '_ws_status', status: 'connected' });
  });

  socket.on('message', (data) => {
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

  socket.on('close', (code, reason) => {
    const reasonText = reason ? reason.toString() : '';
    mainLog(`Engine WebSocket closed (code=${code}, reason=${reasonText || 'none'}).`);
    // D4-09: a superseded socket's close is stale news — it must
    // not tell the renderer the (new) connection dropped, and it
    // must not schedule a reconnect of its own.
    if (wsClient !== socket) return;
    broadcastEngineMessage({ type: '_ws_status', status: 'disconnected' });
    scheduleEngineReconnect();
  });

  socket.on('error', (err) => {
    mainError('WebSocket error:', err);
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
  // D4-02: the saved POSITION is only honoured if the window would
  // actually be visible on a currently-connected display — a
  // monitor unplugged since the last run used to reopen the app
  // off-screen, present but unreachable. The size always survives;
  // only an unusable position is dropped (Electron then centres).
  let bounds = getWindowStateStore().load();
  try {
    bounds = ensureVisibleOnDisplays(bounds, screen.getAllDisplays().map((d) => d.workArea));
  } catch { /* display probing must never block window creation */ }
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
    mainError('Failed to register snip shortcut:', err);
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
  // Unified logging: the launch marker opens this session's slice
  // of app.log (version + platform + pid, per the spec).
  try {
    getAppLog().launchMarker({ version: app.getVersion(), platform: process.platform, pid: process.pid });
  } catch { /* logging never breaks startup */ }
  mainLog('App ready.');
  installSecurityPolicy();
  createWindow();
  // E10-F06: if no engine already answers on the engine port, start
  // the bundled one first (packaged builds); connectEngine() itself
  // is unchanged and its reconnect loop covers every outcome,
  // including the dev case where no payload exists.
  void startEngineAndTrack()
    .finally(() => connectEngine());
  createTray();
  // Epic 6, E6-01: global summon hotkey (Alt+Space toggles the window).
  // D4-10: Electron's register() does NOT throw when the OS refuses
  // the accelerator (another app owns it) — it returns false. The
  // result is checked now, like the snip hotkey's is; a refused
  // summon hotkey is a feature silently missing otherwise.
  try {
    const summonOk = globalShortcut.register('Alt+Space', toggleMainWindow);
    if (!summonOk) {
      mainWarn('Summon hotkey (Alt+Space) could not be registered; another app may be using it.');
    }
  } catch (err) {
    mainError('Failed to register summon hotkey:', err);
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
  mainLog('App quitting (will-quit).');
  // D4-09: no reconnect may be scheduled into a quitting app.
  reconnectGuard.cancel();
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
    mainWarn(`[ipc] Refused '${channel}' from untrusted sender: ${getSenderUrl(event) || '(no URL)'}`);
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
  // E10-02: the folder the on-screen logs actually live in —
  // <userData>/logs, where app.log and engine.log are. The old
  // target (~/.aether/logs) holds only the crash logs, so the
  // button opened a folder without either file the panel shows.
  const dir = path.join(app.getPath('userData'), 'logs');
  fs.mkdirSync(dir, { recursive: true });
  const { shell } = require('electron');
  await shell.openPath(dir);
  return dir;
});
ipcMain.handle('get-ws-status', (event) => {
  if (!isTrustedIpcSender(event, 'get-ws-status')) return false;
  return wsClient && wsClient.readyState === WebSocket.OPEN;
});

// Wave 1 slice 4: startup-gate IPC (see engineStartupState above).
ipcMain.handle('get-engine-state', (event) => {
  if (!isTrustedIpcSender(event, 'get-engine-state')) return { state: 'starting', logFilePath: null };
  const logFilePath = engineLifecycle ? engineLifecycle.logFilePath() : engineStartupState.logFilePath;
  return { ...engineStartupState, logFilePath };
});

// The gate's Retry: only a definitively failed start is retried here
// (the lifecycle refuses nothing itself — start() re-runs from mode
// 'none' — but an engine that is up or starting must not be disturbed).
// Not awaited: a retried cold start can take the full health budget
// twice over; the outcome arrives via the 'engine-state' broadcast.
ipcMain.handle('retry-engine-start', (event) => {
  if (!isTrustedIpcSender(event, 'retry-engine-start')) return engineStartupState;
  if (getEngineLifecycle().getMode() === 'none') {
    void startEngineAndTrack();
  }
  return engineStartupState;
});

// The gate's "Open engine log": opens the slice-3 log file
// (<userData>/logs/engine.log) with the OS default viewer. The
// renderer never learns a path it could fabricate — the location
// comes only from the lifecycle. Resolves to '' on success or the
// shell's error string, like 'open-path'.
ipcMain.handle('open-engine-log', async (event) => {
  if (!isTrustedIpcSender(event, 'open-engine-log')) return 'Refused: untrusted sender.';
  const logPath = engineLifecycle ? engineLifecycle.logFilePath() : null;
  if (!logPath) return 'The engine log location is not known yet.';
  try {
    // A failed start logs lifecycle lines, so the file normally
    // exists; create it empty if it somehow does not, so the open
    // never dead-ends on a missing file.
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    if (!fs.existsSync(logPath)) fs.writeFileSync(logPath, '');
  } catch (err) {
    return `Could not prepare the engine log: ${(err && err.message) || err}`;
  }
  // N-42: even this main-computed path goes through the open-path
  // validator, confined to the userData tree it provably lives in.
  const check = validateOpenPathArg(logPath, {
    roots: [app.getPath('userData')],
    exists: (p) => fs.existsSync(p),
  });
  if (!check.ok) return check.error;
  const { shell } = require('electron');
  return shell.openPath(check.value);
});

// Unified logging IPC (5 Nov 2026): the renderer's [ui] lines and
// the Settings → Diagnostics log viewer / bundle export.
//
// append-ui-log is fire-and-forget (ipcMain.on, like engine-send):
// a renderer logging a line must never block on the write. It is
// rate-capped — a renderer stuck in an error loop must not be able
// to flood the file faster than rotation can bound it — and every
// entry passes validateUiLogEntry, so the renderer can only ever
// write as source 'ui' with a known level and a bounded message.
const UI_LOG_RATE_LIMIT = 120; // entries…
const UI_LOG_RATE_WINDOW_MS = 1000; // …per rolling window
let uiLogWindowStart = 0;
let uiLogWindowCount = 0;

ipcMain.on('append-ui-log', (event, entry) => {
  if (!isTrustedIpcSender(event, 'append-ui-log')) return;
  const nowMs = Date.now();
  if (nowMs - uiLogWindowStart >= UI_LOG_RATE_WINDOW_MS) {
    uiLogWindowStart = nowMs;
    uiLogWindowCount = 0;
  }
  uiLogWindowCount += 1;
  if (uiLogWindowCount > UI_LOG_RATE_LIMIT) return; // dropped silently: logging about logging helps no one
  const check = validateUiLogEntry(entry);
  if (!check.ok) {
    mainWarn(`[ipc] Refused append-ui-log entry: ${check.error}`);
    return;
  }
  try { getAppLog().log(check.value.source, check.value.level, check.value.message); } catch { /* never throws by design */ }
});

// The viewer's data: the tails of both durable logs plus the file
// inventory (name/size/last-write per source). Raw lines — this is
// the user's own machine; redaction applies to the EXPORT below.
ipcMain.handle('read-logs', (event) => {
  if (!isTrustedIpcSender(event, 'read-logs')) return null;
  return {
    appTail: readLogTail(fs, appLogFilePath(), 500),
    engineTail: readLogTail(fs, engineLogFilePath(), 500),
    inventory: logInventory(),
    level: getAppLog().getLevel(),
    // E10-01: the appLog sink's health, surfaced in the panel —
    // a session whose file logging is failing must SAY so
    // somewhere the user can see it.
    logBroken: getAppLog().isBroken(),
  };
});

// Bundle v2, desktop half: everything getDesktopDiagnostics adds
// to the engine bundle at export time. The three tails pass
// through the secret-pattern scrubber (the JS mirror of the
// engine audit redactor's bare-string patterns, appLog.cjs) —
// the bundle is built to be handed to another person.
ipcMain.handle('get-desktop-diagnostics', (event) => {
  if (!isTrustedIpcSender(event, 'get-desktop-diagnostics')) return null;
  return {
    app_version: app.getVersion(),
    platform: process.platform,
    app_log_tail: readLogTail(fs, appLogFilePath(), 500).map((line) => scrubSecrets(line)),
    engine_log_tail: readLogTail(fs, engineLogFilePath(), 500).map((line) => scrubSecrets(line)),
    crash_electron_tail: readLogTail(fs, crashElectronLogPath(), 200).map((line) => scrubSecrets(line)),
    log_inventory: logInventory(),
  };
});

// Clear logs (Settings → Diagnostics, behind the panel's two-step
// confirmation): app.log via the logger (it owns the size cache),
// engine.log by plain truncate. Since D4-01 the lifecycle buffers
// engine.log lines for up to one flush window, so lines buffered
// at the moment of the truncate can still land just after it, and
// its rotation counter over-counts by the truncated size until the
// next run (rotation then fires early — the safe direction).
// Crash logs are NOT cleared — they are the record of last resort.
ipcMain.handle('clear-logs', (event) => {
  if (!isTrustedIpcSender(event, 'clear-logs')) return { ok: false, error: 'Refused: untrusted sender.' };
  const appCleared = getAppLog().clear();
  let engineCleared = true;
  try {
    fs.mkdirSync(path.dirname(engineLogFilePath()), { recursive: true });
    fs.writeFileSync(engineLogFilePath(), '');
  } catch {
    engineCleared = false;
  }
  if (appCleared && engineCleared) {
    return { ok: true };
  }
  return { ok: false, error: 'Some log files could not be cleared.' };
});

// The [main]/[ui] level threshold (Settings → Diagnostics select).
ipcMain.handle('set-log-level', (event, level) => {
  if (!isTrustedIpcSender(event, 'set-log-level')) return { ok: false, level: getAppLog().getLevel() };
  const ok = getAppLog().setLevel(level);
  if (ok) mainLog(`Log level set to ${getAppLog().getLevel()} from Settings.`);
  return { ok, level: getAppLog().getLevel() };
});

ipcMain.on('engine-send', (event, msg) => {
  if (!isTrustedIpcSender(event, 'engine-send')) return;
  const check = validateEngineMessage(msg);
  if (!check.ok) {
    mainWarn(`[ipc] Refused engine-send message: ${check.error}`);
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
    mainWarn(`[ipc] Refused open-floating-chat: ${check.error}`);
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
    mainWarn(`[ipc] Refused show-open-dialog options: ${check.error}`);
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
    mainWarn(`[ipc] Refused snip shortcut: ${check.error}`);
    return { ok: false, shortcut: currentSnipShortcut };
  }
  const ok = registerSnipGlobalShortcut(check.value);
  if (!ok) {
    mainWarn(`[ipc] Snip shortcut '${check.value}' could not be registered; '${currentSnipShortcut}' is still active.`);
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

// The trees 'open-path' may open from (N-42): the workspace roots
// the user selected in the app (user_config in ~/.aether — the
// engine's config file; read from disk here because main owns no
// config cache), plus the app's own data tree (exports, logs,
// attachments land there) and the home/temp trees the OS shell
// integration legitimately targets. Anything outside every root
// is refused before shell.openPath is reached.
function openPathRoots() {
  const roots = [];
  try {
    const configPath = path.join(os.homedir(), '.aether', 'config.json');
    const cfg = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    const userCfg = cfg && typeof cfg === 'object' ? cfg.user_config : null;
    if (userCfg && typeof userCfg === 'object') {
      const candidates = [];
      if (typeof userCfg.workspace_path === 'string') candidates.push(userCfg.workspace_path);
      if (Array.isArray(userCfg.project_workspace_roots)) candidates.push(...userCfg.project_workspace_roots);
      if (Array.isArray(userCfg.workspace_roots)) candidates.push(...userCfg.workspace_roots);
      for (const candidate of candidates) {
        if (typeof candidate === 'string' && candidate.trim().length > 0) roots.push(candidate);
      }
    }
  } catch { /* no config yet, or unreadable — the fixed roots below still apply */ }
  try { roots.push(app.getPath('userData')); } catch { /* pre-ready: skip */ }
  roots.push(os.homedir());
  roots.push(os.tmpdir());
  return roots;
}

// N06: open a file with the OS default application (the canvas
// "Open externally" action). Resolves to '' on success or the
// shell's error string — the renderer surfaces it, never a picker.
// N-42: validation is no longer shape-only — the path must be
// absolute, must exist, must sit under one of the app's own roots
// (openPathRoots), and is never an executable/script type
// (electron/ipcValidation.cjs).
ipcMain.handle('open-path', async (event, filePath) => {
  if (!isTrustedIpcSender(event, 'open-path')) return 'Refused: untrusted sender.';
  const check = validateOpenPathArg(filePath, { roots: openPathRoots(), exists: (p) => fs.existsSync(p) });
  if (!check.ok) return check.error;
  const { shell } = require('electron');
  return shell.openPath(check.value);
});
