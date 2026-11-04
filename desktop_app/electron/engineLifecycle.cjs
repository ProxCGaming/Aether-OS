'use strict';

// E10-F06 — bundled-engine lifecycle for the packaged app.
//
// The distributable carries the Python engine as FILES (portable
// interpreter + engine code; see scripts/assemble_engine_payload.py
// and the layout contract in scripts/engine_payload_layout.json).
// This module owns the spawned engine's life:
//
//   start()  1. Probe http://127.0.0.1:8000/health briefly. An engine
//               that already answers is EXTERNAL — connect exactly as
//               the shell always has (developer flow untouched, and
//               the probe doubles as the second-instance guard: the
//               first instance's engine is already there, so a second
//               app process never spawns a duplicate).
//            2. No answer + a bundled payload present: spawn the
//               payload interpreter on the engine entry module, wait
//               for /health (bounded), then report 'spawned'.
//            3. No answer + no payload (dev checkout): report 'none'
//               and the shell keeps its existing connect/retry
//               behaviour — nothing regresses.
//
//   Token agreement (S1-M3): the engine GENERATES its auth token at
//   startup and writes it to `.run/engine.token` relative to ITS OWN
//   working directory (aether_common/auth.py). The spawned engine's
//   CWD is this app's per-user run directory, so the token file lands
//   at <userData>/engine-run/.run/engine.token — tokenFilePath()
//   names exactly that file once an engine has been spawned, and
//   main.cjs reads it the same way it reads the dev token file. The
//   token still only ever travels as an Authorization header, held
//   by the main process; the renderer never sees it.
//
//   Supervision: an unexpected exit of the spawned engine triggers a
//   bounded restart (MAX_RESTARTS, visible below — the new engine
//   writes a fresh token and the shell's existing reconnect loop
//   picks it up). stop() terminates the child on app quit: SIGTERM,
//   a grace period, then SIGKILL (POSIX) / taskkill /T /F (Windows,
//   so the engine's own worker children go with it).
//
//   Wave 1 slice 3 hardening: the INITIAL spawn is retried once if
//   it misses the health budget (INITIAL_SPAWN_ATTEMPTS), a healthy
//   respawn resets the restart budget (RUX-B-11 — the bound means
//   consecutive failed restarts, not restarts per session), and
//   every lifecycle line is also appended to <userData>/logs/
//   engine.log, the copy a user or QA can actually retrieve from a
//   packaged app whose console nobody can see.
//
// All decisions live here with injected dependencies so Jest can
// drive them without Electron; main.cjs is wiring only (its half is
// pinned by source-contract assertions in the tests).

const path = require('path');

// --- Layout constants -------------------------------------------------
// Mirror of scripts/engine_payload_layout.json — THE contract. The
// Jest suite enginePayloadLayout.test.js fails if these drift from
// that file; change the layout there first.
const ENGINE_PORT = 8000;
const HEALTH_PATH = '/health';
const PAYLOAD_DIR_NAME = 'engine';
const RUNTIME_DIR = 'python';
const APP_DIR = 'app';
const ENTRY_MODULE = 'aether_engine.main';
const MANIFEST_FILE = 'payload-manifest.json';
const PYTHON_EXE_BY_PLATFORM = {
  win32: 'python.exe',
  linux: path.join('bin', 'python3'),
  darwin: path.join('bin', 'python3'),
};
// ----------------------------------------------------------------------

const PROBE_TIMEOUT_MS = 750;
// 120s, raised from 30s on 4 Nov 2026: a cold first start on Windows
// (antivirus scanning the freshly laid payload tree, first-run .pyc
// generation, heavy imports, and an engine lifespan that still awaits
// provider discovery) can legitimately exceed 30s — and the previous
// behaviour on timeout was to KILL the healthy-but-slow engine and
// never spawn again, leaving the app at "initialising" forever.
const HEALTH_TIMEOUT_MS = 120000;
const HEALTH_POLL_MS = 250;
// The restart bound: a spawned engine that keeps dying is not
// restarted forever. After this many restarts the shell gives up
// (and stays in its normal disconnected state) instead of spinning.
const MAX_RESTARTS = 3;
const RESTART_DELAY_MS = 1000;
const KILL_GRACE_MS = 5000;
// The initial spawn gets ONE retry (two attempts total, never a
// loop). A cold first start can miss the health budget for transient
// reasons — antivirus still scanning the freshly laid payload tree,
// a port briefly held — that are gone moments later; the pre-fix
// behaviour terminated the child and gave up for the whole session,
// leaving a dead "Initializing" UI (Lane B §4, point 3).
const INITIAL_SPAWN_ATTEMPTS = 2;
const SPAWN_RETRY_DELAY_MS = 1000;

function createEngineLifecycle(deps) {
  const {
    spawn,
    httpGet,
    fs,
    platform,
    isPackaged,
    resourcesPath,
    userDataPath,
    baseEnv = {},
    log = () => {},
  } = deps;
  const tuning = deps.tuning || {};
  const probeTimeoutMs = tuning.probeTimeoutMs ?? PROBE_TIMEOUT_MS;
  const healthTimeoutMs = tuning.healthTimeoutMs ?? HEALTH_TIMEOUT_MS;
  const healthPollMs = tuning.healthPollMs ?? HEALTH_POLL_MS;
  const maxRestarts = tuning.maxRestarts ?? MAX_RESTARTS;
  const restartDelayMs = tuning.restartDelayMs ?? RESTART_DELAY_MS;
  const killGraceMs = tuning.killGraceMs ?? KILL_GRACE_MS;
  const spawnRetryDelayMs = tuning.spawnRetryDelayMs ?? SPAWN_RETRY_DELAY_MS;
  const timers = deps.timers || { setTimeout, clearTimeout };
  const now = deps.now || Date.now;

  let mode = null; // null | 'external' | 'spawned' | 'none' | 'stopped'
  let child = null;
  let childExited = false;
  let restartCount = 0;
  let restartTimer = null;
  let stopping = false;
  let startPromise = null;
  let exitWaiters = [];

  // Engine log file (Wave 1 slice 3): every lifecycle line ALSO
  // lands in <userData>/logs/engine.log. The injected log sink is
  // console.log in the packaged main process — a GUI process whose
  // console nobody can see (Lane B §4, point 6) — so the file is
  // the copy a user or QA can retrieve. Best-effort by
  // construction: the directory is created lazily, a write failure
  // is swallowed and disables file logging, and it must NEVER throw
  // into lifecycle logic.
  const engineLogFilePath = userDataPath
    ? path.join(userDataPath, 'logs', 'engine.log')
    : null;
  let engineLogReady = false;
  let engineLogBroken = false;

  function appendToEngineLog(message) {
    if (!engineLogFilePath || engineLogBroken) return;
    try {
      if (!engineLogReady) {
        fs.mkdirSync(path.dirname(engineLogFilePath), { recursive: true });
        engineLogReady = true;
      }
      const stamp = new Date(now()).toISOString();
      const body = String(message)
        .split('\n')
        .map((line) => `${stamp} ${line}`)
        .join('\n');
      fs.appendFileSync(engineLogFilePath, `${body}\n`);
    } catch {
      engineLogBroken = true;
    }
  }

  // The one sink every lifecycle message in this module goes
  // through: the injected log exactly as before, plus the durable
  // file copy above.
  function logLine(message) {
    log(message);
    appendToEngineLog(message);
  }

  const sleep = (ms) => new Promise((resolve) => timers.setTimeout(resolve, ms));

  function healthUrl() {
    return `http://127.0.0.1:${ENGINE_PORT}${HEALTH_PATH}`;
  }

  async function probe() {
    try {
      const status = await httpGet(healthUrl(), probeTimeoutMs);
      return status === 200;
    } catch {
      return false;
    }
  }

  function resolvePayload() {
    if (!isPackaged || !resourcesPath) return null;
    const exeRel = PYTHON_EXE_BY_PLATFORM[platform];
    if (!exeRel) return null;
    const root = path.join(resourcesPath, PAYLOAD_DIR_NAME);
    const pythonExe = path.join(root, RUNTIME_DIR, exeRel);
    const appDir = path.join(root, APP_DIR);
    try {
      if (!fs.existsSync(pythonExe) || !fs.existsSync(appDir)) return null;
    } catch {
      return null;
    }
    return { root, pythonExe, appDir };
  }

  function runDir() {
    return path.join(userDataPath, 'engine-run');
  }

  // The token file of the engine WE spawned (its CWD is runDir()).
  // Null when nothing was spawned — main.cjs then uses the existing
  // repo-relative dev token path, byte-for-byte as before.
  function tokenFilePath() {
    if (mode !== 'spawned' && !child) return null;
    return path.join(runDir(), '.run', 'engine.token');
  }

  async function waitForHealth(spawnedChild) {
    const deadline = now() + healthTimeoutMs;
    for (;;) {
      if (stopping) return false;
      if (spawnedChild && spawnedChild === child && childExited) return false;
      if (await probe()) return true;
      if (now() >= deadline) return false;
      await sleep(healthPollMs);
    }
  }

  function spawnEngine(payload) {
    const cwd = runDir();
    try {
      fs.mkdirSync(cwd, { recursive: true });
    } catch (err) {
      logLine(`[engine] could not create engine run directory ${cwd}: ${err}`);
      return null;
    }
    const env = { ...baseEnv, PYTHONUNBUFFERED: '1' };
    env.PYTHONPATH = env.PYTHONPATH
      ? payload.appDir + path.delimiter + env.PYTHONPATH
      : payload.appDir;
    let proc;
    try {
      proc = spawn(payload.pythonExe, ['-m', ENTRY_MODULE], {
        cwd,
        env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      logLine(`[engine] failed to spawn bundled engine: ${err}`);
      return null;
    }
    child = proc;
    childExited = false;
    if (proc.stdout && typeof proc.stdout.on === 'function') {
      proc.stdout.on('data', (chunk) => logLine(`[engine] ${String(chunk).trimEnd()}`));
    }
    if (proc.stderr && typeof proc.stderr.on === 'function') {
      proc.stderr.on('data', (chunk) => logLine(`[engine] ${String(chunk).trimEnd()}`));
    }
    if (typeof proc.on === 'function') {
      proc.on('error', (err) => logLine(`[engine] process error: ${err}`));
      proc.on('exit', (code, signal) => onChildExit(proc, code, signal));
    }
    logLine(`[engine] spawned bundled engine (pid ${proc.pid}) from ${payload.root}`);
    return proc;
  }

  function onChildExit(proc, code, signal) {
    if (proc !== child) return; // stale process from an earlier spawn
    childExited = true;
    const waiters = exitWaiters;
    exitWaiters = [];
    waiters.forEach((resolve) => resolve());
    if (stopping) return;
    logLine(`[engine] bundled engine exited (code=${code}, signal=${signal})`);
    if (mode === 'spawned') {
      scheduleRestart();
    }
  }

  function scheduleRestart() {
    if (stopping) return;
    if (restartCount >= maxRestarts) {
      logLine(`[engine] restart bound reached (${maxRestarts}); not restarting the bundled engine`);
      mode = 'none';
      child = null;
      return;
    }
    restartCount += 1;
    logLine(`[engine] restarting bundled engine (attempt ${restartCount}/${maxRestarts})`);
    restartTimer = timers.setTimeout(() => {
      restartTimer = null;
      void respawn();
    }, restartDelayMs);
  }

  async function respawn() {
    if (stopping) return;
    const payload = resolvePayload();
    if (!payload) {
      mode = 'none';
      return;
    }
    const proc = spawnEngine(payload);
    if (!proc) {
      scheduleRestart();
      return;
    }
    const healthy = await waitForHealth(proc);
    if (stopping) return;
    if (healthy) {
      mode = 'spawned';
      // RUX-B-11: a respawn that becomes healthy earns the budget
      // back (mirrors the reset in start()). Without this the bound
      // was cumulative per session, so three individually recovered
      // crashes permanently ended supervision.
      restartCount = 0;
      logLine('[engine] bundled engine is healthy after restart');
    } else {
      logLine('[engine] restarted engine did not become healthy');
      await terminateChild(proc);
      if (!stopping && mode === 'spawned') scheduleRestart();
    }
  }

  function waitForExit(ms) {
    if (!child || childExited) return Promise.resolve(true);
    return new Promise((resolve) => {
      const timer = timers.setTimeout(() => resolve(false), ms);
      exitWaiters.push(() => {
        timers.clearTimeout(timer);
        resolve(true);
      });
    });
  }

  async function terminateChild(proc) {
    if (!proc || (proc === child && childExited)) return;
    const isCurrent = proc === child;
    try { proc.kill('SIGTERM'); } catch { /* already gone */ }
    const exited = await waitForExit(killGraceMs);
    if (exited) return;
    if (platform === 'win32') {
      // The engine spawns its own worker children; only a tree kill
      // takes the whole family down on Windows.
      try {
        const killer = spawn('taskkill', ['/pid', String(proc.pid), '/T', '/F'], { windowsHide: true });
        if (killer && typeof killer.on === 'function') {
          await new Promise((resolve) => {
            killer.on('exit', resolve);
            killer.on('error', resolve);
            timers.setTimeout(resolve, killGraceMs);
          });
        }
      } catch (err) {
        logLine(`[engine] taskkill failed: ${err}`);
      }
    } else {
      try { proc.kill('SIGKILL'); } catch { /* already gone */ }
      await waitForExit(killGraceMs);
    }
    if (isCurrent && !childExited) {
      logLine('[engine] bundled engine did not exit within the kill grace period');
    }
  }

  async function start() {
    if (startPromise) return startPromise;
    if (mode === 'external' || mode === 'spawned') return { mode };
    startPromise = (async () => {
      if (await probe()) {
        mode = 'external';
        logLine('[engine] an engine already answers on port ' + ENGINE_PORT + '; using it');
        return { mode };
      }
      const payload = resolvePayload();
      if (!payload) {
        mode = 'none';
        logLine('[engine] no engine is running and no bundled engine payload is present');
        return { mode };
      }
      // Wave 1 slice 3: the initial spawn is retried ONCE when it
      // misses the health budget (see INITIAL_SPAWN_ATTEMPTS). The
      // failed child is terminated exactly as the single-attempt
      // path always did before the next attempt. A spawn that never
      // produced a child (mkdir/spawn failure) is deterministic —
      // it still gives up immediately, unchanged.
      for (let attempt = 1; attempt <= INITIAL_SPAWN_ATTEMPTS; attempt += 1) {
        const proc = spawnEngine(payload);
        if (!proc) {
          mode = 'none';
          return { mode };
        }
        if (await waitForHealth(proc)) {
          mode = 'spawned';
          restartCount = 0;
          logLine('[engine] bundled engine is healthy');
          return { mode };
        }
        await terminateChild(proc);
        if (child === proc) child = null;
        if (stopping || attempt === INITIAL_SPAWN_ATTEMPTS) break;
        logLine(`[engine] bundled engine did not become healthy in time; retrying the spawn (attempt ${attempt + 1} of ${INITIAL_SPAWN_ATTEMPTS})`);
        await sleep(spawnRetryDelayMs);
      }
      logLine('[engine] bundled engine did not become healthy in time; falling back to the normal connection retry');
      mode = 'none';
      return { mode };
    })();
    try {
      return await startPromise;
    } finally {
      startPromise = null;
    }
  }

  async function stop() {
    stopping = true;
    if (restartTimer) {
      timers.clearTimeout(restartTimer);
      restartTimer = null;
    }
    const proc = child;
    if (proc) {
      await terminateChild(proc);
      if (child === proc) child = null;
    }
    mode = 'stopped';
  }

  // Last-resort synchronous kill for the process 'exit' event, where
  // no async work can run. will-quit (stop()) is the primary path;
  // this only covers abrupt exits.
  function killSync() {
    if (child && !childExited) {
      try { child.kill('SIGKILL'); } catch { /* best effort */ }
    }
  }

  return {
    start,
    stop,
    killSync,
    probe,
    resolvePayload,
    tokenFilePath,
    logFilePath: () => engineLogFilePath,
    runDir,
    getMode: () => mode,
    isSpawned: () => mode === 'spawned',
  };
}

module.exports = {
  createEngineLifecycle,
  ENGINE_PORT,
  HEALTH_PATH,
  PAYLOAD_DIR_NAME,
  RUNTIME_DIR,
  APP_DIR,
  ENTRY_MODULE,
  MANIFEST_FILE,
  PYTHON_EXE_BY_PLATFORM,
  PROBE_TIMEOUT_MS,
  HEALTH_TIMEOUT_MS,
  MAX_RESTARTS,
  RESTART_DELAY_MS,
  KILL_GRACE_MS,
  INITIAL_SPAWN_ATTEMPTS,
  SPAWN_RETRY_DELAY_MS,
};
