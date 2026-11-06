/**
 * E10-F06 — bundled-engine lifecycle decisions.
 *
 * electron/engineLifecycle.cjs owns every decision (probe → spawn →
 * health → supervise → terminate); main.cjs is wiring only and
 * cannot be required in Jest, so — like transferState.test.js — the
 * main-process half is pinned by source-contract assertions below.
 *
 * The fakes mirror the real seams: an EventEmitter child process
 * (pid / kill / stdout / stderr), an httpGet that answers from a
 * mutable "is the engine up" flag, and an fs whose existsSync knows
 * only the paths each scenario plants.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { EventEmitter } = require('events');

const {
  createEngineLifecycle,
  MAX_RESTARTS,
  INITIAL_SPAWN_ATTEMPTS,
} = require('../engineLifecycle.cjs');

const appRoot = path.join(__dirname, '..', '..');
const mainSrc = fs.readFileSync(path.join(appRoot, 'main.cjs'), 'utf8');

const RESOURCES = path.join(path.sep, 'fake', 'resources');
const USER_DATA = path.join(path.sep, 'fake', 'userData');
const PAYLOAD_EXE = path.join(RESOURCES, 'engine', 'python', 'bin', 'python3');
const PAYLOAD_APP = path.join(RESOURCES, 'engine', 'app');
const PAYLOAD_MANIFEST = path.join(RESOURCES, 'engine', 'payload-manifest.json');
const RUN_DIR = path.join(USER_DATA, 'engine-run');

// A manifest the assembler's write_manifest would produce for a
// complete payload (D4-18: the launcher now verifies it at spawn).
function validManifest(platform) {
  return JSON.stringify({
    format: 1,
    platform,
    python_version: '3.12.16',
    runtime_source: 'python-build-standalone 20261026 (test)',
    runtime_dir: 'python',
    app_dir: 'app',
    app_packages: ['aether_engine', 'aether_common', 'aether_worker'],
    entry_module: 'aether_engine.main',
    assembled_at: '2026-11-06T00:00:00Z',
    assembled_by: 'scripts/assemble_engine_payload.py',
  });
}

function fakeChild(pid, { dieOnTerm = false } = {}) {
  const child = new EventEmitter();
  child.pid = pid;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.killCalls = [];
  child.kill = (signal) => {
    child.killCalls.push(signal);
    if (dieOnTerm && signal === 'SIGTERM') {
      setTimeout(() => child.emit('exit', null, 'SIGTERM'), 1);
    }
    return true;
  };
  child.die = (code = 1, signal = null) => child.emit('exit', code, signal);
  return child;
}

function makeLifecycle(overrides = {}) {
  const state = {
    healthUp: false,
    existing: new Set(overrides.existing || []),
    spawnCalls: [],
    children: [],
    mkdirCalls: [],
    logs: [],
    // D4-18: a planted payload app dir implies its package dirs,
    // as a real assembled payload has them.
    manifestContent: overrides.manifest === undefined
      ? validManifest(overrides.platform || 'linux')
      : overrides.manifest,
  };
  if (state.existing.has(PAYLOAD_APP)) {
    for (const pkg of ['aether_engine', 'aether_common', 'aether_worker']) {
      state.existing.add(path.join(PAYLOAD_APP, pkg));
    }
  }
  let nextPid = 4100;
  const deps = {
    spawn: jest.fn((exe, args, options) => {
      state.spawnCalls.push({ exe, args, options });
      if (exe === 'taskkill') {
        const killer = new EventEmitter();
        killer.pid = 9999;
        killer.kill = () => true;
        setTimeout(() => killer.emit('exit', 0, null), 1);
        return killer;
      }
      const child = fakeChild(nextPid++, overrides.childOptions || {});
      state.children.push(child);
      // engineBoots === false: no spawn ever brings the engine up.
      // respawnBoots === false: only the FIRST spawn brings it up;
      // respawns stay down (restarts that never recover). Default:
      // every spawn brings it up, as before.
      if (overrides.engineBoots === false) {
        state.healthUp = false;
      } else if (state.children.length > 1 && overrides.respawnBoots === false) {
        state.healthUp = false;
      } else {
        state.healthUp = true;
      }
      return child;
    }),
    httpGet: jest.fn(async () => {
      if (!state.healthUp) throw new Error('ECONNREFUSED');
      return 200;
    }),
    fs: {
      existsSync: (p) => state.existing.has(p),
      mkdirSync: (p, opts) => { state.mkdirCalls.push({ p, opts }); },
      readFileSync: (p) => {
        if (p === PAYLOAD_MANIFEST && state.manifestContent !== null) {
          return state.manifestContent;
        }
        throw new Error(`ENOENT: ${p}`);
      },
    },
    platform: overrides.platform || 'linux',
    isPackaged: overrides.isPackaged !== false,
    resourcesPath: RESOURCES,
    userDataPath: USER_DATA,
    baseEnv: { PATH: '/usr/bin' },
    log: (msg) => state.logs.push(String(msg)),
    tuning: {
      healthTimeoutMs: 150,
      healthPollMs: 5,
      restartDelayMs: 5,
      killGraceMs: overrides.killGraceMs ?? 25,
      spawnRetryDelayMs: 5,
      maxRestarts: overrides.maxRestarts ?? 2,
    },
  };
  return { lifecycle: createEngineLifecycle(deps), deps, state };
}

const tick = (ms = 40) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(cond, timeoutMs = 2000) {
  const start = Date.now();
  for (;;) {
    if (cond()) return;
    if (Date.now() - start > timeoutMs) throw new Error('until() timed out');
    await tick(5);
  }
}

describe('engineLifecycle.start', () => {
  it('uses an already-running engine and never spawns (developer flow / second-instance guard)', async () => {
    const { lifecycle, deps, state } = makeLifecycle();
    state.healthUp = true;
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'external' });
    expect(deps.spawn).not.toHaveBeenCalled();
    expect(lifecycle.tokenFilePath()).toBeNull();
  });

  it('reports none on a dev checkout (not packaged) with no engine running', async () => {
    const { lifecycle, deps } = makeLifecycle({ isPackaged: false });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    expect(deps.spawn).not.toHaveBeenCalled();
  });

  it('reports none when packaged but the payload is absent', async () => {
    const { lifecycle, deps } = makeLifecycle();
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    expect(deps.spawn).not.toHaveBeenCalled();
  });

  it('spawns the bundled engine with the layout coordinates and waits for health', async () => {
    const { lifecycle, deps, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
    });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'spawned' });
    expect(deps.spawn).toHaveBeenCalledTimes(1);
    const call = state.spawnCalls[0];
    expect(call.exe).toBe(PAYLOAD_EXE);
    expect(call.args).toEqual(['-m', 'aether_engine.main']);
    expect(call.options.cwd).toBe(RUN_DIR);
    expect(call.options.env.PYTHONPATH.split(path.delimiter)[0]).toBe(PAYLOAD_APP);
    expect(call.options.env.PYTHONUNBUFFERED).toBe('1');
    expect(state.mkdirCalls).toContainEqual({ p: RUN_DIR, opts: { recursive: true } });
    // The token agreement: the spawned engine writes
    // .run/engine.token under its CWD; main reads exactly there.
    expect(lifecycle.tokenFilePath()).toBe(path.join(RUN_DIR, '.run', 'engine.token'));
    expect(lifecycle.isSpawned()).toBe(true);
    // Idempotent: a second start() does not spawn again.
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'spawned' });
    expect(deps.spawn).toHaveBeenCalledTimes(1);
  });

  it('gives up cleanly when the spawned engine never becomes healthy', async () => {
    const { lifecycle, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      engineBoots: false,
    });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    const child = state.children[0];
    expect(child.killCalls).toEqual(['SIGTERM', 'SIGKILL']);
    expect(lifecycle.tokenFilePath()).toBeNull();
  });
});

describe('engineLifecycle supervision', () => {
  it('restarts a crashed engine only up to the bound, then gives up', async () => {
    // Wave 1 slice 3 / RUX-B-11 semantics: the bound counts
    // CONSECUTIVE FAILED restarts — a respawn that becomes healthy
    // resets it (see the RUX-B-11 test below). So the bound is
    // exercised here with respawns that never recover.
    const { lifecycle, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      maxRestarts: 2,
      respawnBoots: false,
    });
    await lifecycle.start();
    expect(state.children).toHaveLength(1);
    state.children[0].die(1);
    await until(() => lifecycle.getMode() === 'none', 4000);
    expect(state.children).toHaveLength(3); // 1 initial + 2 failed restarts, no more
    expect(state.logs.join('\n')).toMatch(/restart bound reached/);
  });

  it('RUX-B-11: a respawn that becomes healthy resets the restart budget', async () => {
    const { lifecycle, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      maxRestarts: 1, // tightest bound: one recovered crash used to end supervision
    });
    await lifecycle.start();
    state.children[0].die(1);
    await until(() => state.children.length === 2);
    await until(() => state.logs.join('\n').includes('healthy after restart'));
    state.children[1].die(1);
    // Before the fix the budget was cumulative per session, so
    // this second crash hit the bound and no third child ever
    // spawned. With the reset, the restart is allowed.
    await until(() => state.children.length === 3);
    expect(lifecycle.getMode()).toBe('spawned');
    expect(state.logs.join('\n')).not.toMatch(/restart bound reached/);
  });

  it('ships a visible production restart bound', () => {
    expect(MAX_RESTARTS).toBe(3);
  });
});

describe('engineLifecycle.stop', () => {
  it('SIGTERMs the spawned engine and does not restart it afterwards', async () => {
    const { lifecycle, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      childOptions: { dieOnTerm: true },
    });
    await lifecycle.start();
    await lifecycle.stop();
    const child = state.children[0];
    expect(child.killCalls).toEqual(['SIGTERM']);
    await tick(60);
    expect(state.children).toHaveLength(1);
    expect(lifecycle.getMode()).toBe('stopped');
    expect(lifecycle.tokenFilePath()).toBeNull();
  });

  it('tree-kills with taskkill on Windows when SIGTERM is not enough', async () => {
    const winExe = path.join(RESOURCES, 'engine', 'python', 'python.exe');
    const { lifecycle, state } = makeLifecycle({
      platform: 'win32',
      existing: [winExe, PAYLOAD_APP],
    });
    await lifecycle.start();
    await lifecycle.stop();
    const taskkill = state.spawnCalls.find((c) => c.exe === 'taskkill');
    expect(taskkill).toBeDefined();
    expect(taskkill.args).toEqual(['/pid', String(state.children[0].pid), '/T', '/F']);
  });

  it('killSync delivers a last-resort SIGKILL', async () => {
    const { lifecycle, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
    });
    await lifecycle.start();
    lifecycle.killSync();
    expect(state.children[0].killCalls).toContain('SIGKILL');
  });
});

describe('main.cjs wiring (source contract)', () => {
  it('requires the lifecycle module and starts it before connecting', () => {
    expect(mainSrc).toContain("require('./electron/engineLifecycle.cjs')");
    expect(mainSrc).toContain('getEngineLifecycle().start()');
    expect(mainSrc).toContain('.finally(() => connectEngine())');
    expect(mainSrc).toContain('process.resourcesPath');
    expect(mainSrc).toContain('app.isPackaged');
  });

  it('stops the spawned engine on the quit path', () => {
    expect(mainSrc).toContain('engineLifecycle.stop()');
    expect(mainSrc).toContain("app.on('will-quit'");
    expect(mainSrc).toContain('engineLifecycle.killSync()');
    expect(mainSrc).toContain("process.on('exit'");
    // No 'before-quit' handler: F-14 pins its absence (a
    // preventDefault there could cancel the quit short of
    // will-quit); the engine stop rides will-quit + the exit
    // backstop instead.
    expect(mainSrc).not.toContain("'before-quit'");
  });

  it('reads the spawned engine token file, keeping the dev token path and header-only transport', () => {
    expect(mainSrc).toContain('engineLifecycle.tokenFilePath()');
    expect(mainSrc).toContain("'.run', 'engine.token'");
    expect(mainSrc).toContain('Authorization: `Bearer ${token}`');
    // The URL itself stays a bare literal — the token is never
    // interpolated into it (Phase 10.2 B1).
    expect(mainSrc).toContain("const wsUrl = 'ws://127.0.0.1:8000/ws/tasks';");
  });
});

describe('engineLifecycle initial-spawn retry (Wave 1 slice 3)', () => {
  it('retries the initial spawn once and succeeds when the second attempt becomes healthy', async () => {
    const { lifecycle, deps, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      engineBoots: false, // the test flips healthUp by hand
    });
    const started = lifecycle.start();
    // First attempt never comes up; wait for its replacement to be
    // spawned, then let THAT one answer /health.
    await until(() => state.children.length === 2);
    state.healthUp = true;
    await expect(started).resolves.toEqual({ mode: 'spawned' });
    expect(deps.spawn).toHaveBeenCalledTimes(2);
    // The failed first child was terminated before the retry.
    expect(state.children[0].killCalls).toContain('SIGTERM');
    expect(state.logs.join('\n')).toMatch(/retrying the spawn \(attempt 2 of 2\)/);
  });

  it('gives up after exactly two attempts when neither becomes healthy', async () => {
    const { lifecycle, deps, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      engineBoots: false,
    });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    expect(deps.spawn).toHaveBeenCalledTimes(2);
    expect(state.children).toHaveLength(2);
    expect(state.children[1].killCalls).toEqual(['SIGTERM', 'SIGKILL']);
  });

  it('ships a visible single-retry bound for the initial spawn', () => {
    expect(INITIAL_SPAWN_ATTEMPTS).toBe(2);
  });
});

describe('engineLifecycle engine log file (Wave 1 slice 3)', () => {
  // Real fs against tmp dirs: the file sink is the behaviour under
  // test, so this block does not use makeLifecycle's fake fs.
  function makeRealLifecycle(userDataPath) {
    const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'aether-res-'));
    const exe = path.join(resourcesPath, 'engine', 'python', 'bin', 'python3');
    fs.mkdirSync(path.dirname(exe), { recursive: true });
    fs.writeFileSync(exe, '');
    // D4-18: a real payload carries its package dirs and manifest.
    for (const pkg of ['aether_engine', 'aether_common', 'aether_worker']) {
      fs.mkdirSync(path.join(resourcesPath, 'engine', 'app', pkg), { recursive: true });
    }
    fs.writeFileSync(
      path.join(resourcesPath, 'engine', 'payload-manifest.json'),
      validManifest('linux'),
    );
    const children = [];
    let up = false;
    const lifecycle = createEngineLifecycle({
      spawn: jest.fn(() => {
        const child = fakeChild(6000 + children.length);
        children.push(child);
        up = true;
        return child;
      }),
      httpGet: jest.fn(async () => {
        if (!up) throw new Error('ECONNREFUSED');
        return 200;
      }),
      fs,
      platform: 'linux',
      isPackaged: true,
      resourcesPath,
      userDataPath,
      baseEnv: {},
      log: () => {},
      tuning: {
        healthTimeoutMs: 150,
        healthPollMs: 5,
        restartDelayMs: 5,
        killGraceMs: 25,
        spawnRetryDelayMs: 5,
        maxRestarts: 2,
      },
    });
    return { lifecycle, children };
  }

  it('appends timestamped lifecycle lines to <userData>/logs/engine.log', async () => {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'aether-ud-'));
    const { lifecycle, children } = makeRealLifecycle(userData);
    const logPath = path.join(userData, 'logs', 'engine.log');
    expect(lifecycle.logFilePath()).toBe(logPath);
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'spawned' });
    // D4-01: appends are buffered and flushed asynchronously, so
    // poll for the flush instead of reading synchronously (the
    // assertions on content are unchanged).
    await until(() => fs.existsSync(logPath)
      && fs.readFileSync(logPath, 'utf8').includes('bundled engine is healthy'));
    const content = fs.readFileSync(logPath, 'utf8');
    expect(content).toMatch(
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z \[engine\] spawned bundled engine/,
    );
    expect(content).toContain('[engine] bundled engine is healthy');
    // Later lifecycle events append to the same file.
    children[0].die(1);
    await until(() => fs.readFileSync(logPath, 'utf8').includes('healthy after restart'));
    const after = fs.readFileSync(logPath, 'utf8');
    expect(after).toContain('[engine] bundled engine exited (code=1, signal=null)');
    expect(after.length).toBeGreaterThan(content.length);
  });

  it('rotates engine.log past 5 MB, keeping the old content as .1', async () => {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'aether-ud-'));
    const logDir = path.join(userData, 'logs');
    fs.mkdirSync(logDir, { recursive: true });
    const logPath = path.join(logDir, 'engine.log');
    // Seed an oversized pre-rotation log, as a long-lived install
    // from before rotation existed would have.
    fs.writeFileSync(logPath, `${'x'.repeat(1024)}\n`.repeat(5200));
    const { lifecycle } = makeRealLifecycle(userData);
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'spawned' });
    const archived = fs.readFileSync(`${logPath}.1`, 'utf8');
    expect(archived.length).toBeGreaterThan(5 * 1024 * 1024);
    const live = fs.readFileSync(logPath, 'utf8');
    expect(live).toContain('[engine] spawned bundled engine');
    expect(live.length).toBeLessThan(1024 * 1024);
  });

  it('an unwritable log path never breaks start()', async () => {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'aether-ud-'));
    // A regular FILE where the logs directory must be created:
    // mkdir/append under it fails, and the lifecycle must swallow
    // that and carry on exactly as before.
    fs.writeFileSync(path.join(userData, 'logs'), 'not a directory');
    const { lifecycle } = makeRealLifecycle(userData);
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'spawned' });
    expect(lifecycle.isSpawned()).toBe(true);
  });
});

describe('engineLifecycle payload manifest verification (D4-18)', () => {
  it('refuses a payload whose manifest is missing — a partial tree never spawns', async () => {
    const { lifecycle, deps, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      manifest: null,
    });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    expect(deps.spawn).not.toHaveBeenCalled();
    expect(state.logs.join('\n')).toMatch(/manifest/);
  });

  it('refuses a payload whose manifest is not parseable JSON', async () => {
    const { lifecycle, deps } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      manifest: '{not json',
    });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    expect(deps.spawn).not.toHaveBeenCalled();
  });

  it('refuses a payload assembled for a different platform', async () => {
    const { lifecycle, deps } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      manifest: validManifest('win32'), // lifecycle platform is linux
    });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    expect(deps.spawn).not.toHaveBeenCalled();
  });

  it('refuses a payload whose manifest lists an app package the tree lacks', async () => {
    const manifest = JSON.parse(validManifest('linux'));
    manifest.app_packages = ['aether_engine', 'aether_common', 'ghost_package'];
    const { lifecycle, deps } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      manifest: JSON.stringify(manifest),
    });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    expect(deps.spawn).not.toHaveBeenCalled();
  });

  it('refuses a payload whose manifest names a different entry module', async () => {
    const manifest = JSON.parse(validManifest('linux'));
    manifest.entry_module = 'some.other.module';
    const { lifecycle, deps } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      manifest: JSON.stringify(manifest),
    });
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'none' });
    expect(deps.spawn).not.toHaveBeenCalled();
  });
});

describe('engineLifecycle terminateChild (D4-18)', () => {
  it('waits on the process it was given, not the current child', async () => {
    const { lifecycle, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      killGraceMs: 800,
    });
    await lifecycle.start(); // a current child exists and stays alive
    const stranger = fakeChild(9999, { dieOnTerm: true });
    const startedAt = Date.now();
    await lifecycle.terminateChild(stranger);
    const elapsed = Date.now() - startedAt;
    // The stranger exited on SIGTERM: no escalation, no grace wait.
    expect(stranger.killCalls).toEqual(['SIGTERM']);
    expect(elapsed).toBeLessThan(400);
    expect(state.logs.join('\n')).not.toMatch(/did not exit/);
    await lifecycle.stop();
  });
});

describe('main.cjs shell hardening wiring (Wave 3)', () => {
  it('clamps restored window bounds against the connected displays (D4-02)', () => {
    expect(mainSrc).toContain('ensureVisibleOnDisplays');
    expect(mainSrc).toContain('screen.getAllDisplays()');
  });

  it('reconnects through the guarded reconnect helper, not a bare timer (D4-09)', () => {
    expect(mainSrc).toContain('scheduleEngineReconnect()');
    expect(mainSrc).not.toContain('setTimeout(connectEngine, 3000); // auto reconnect');
    expect(mainSrc).toContain('reconnectGeneration');
  });

  it('does not open a tokenless socket against the engine (D4-09 / N-42 family)', () => {
    expect(mainSrc).toContain('No engine token is available yet');
  });

  it('checks the Alt+Space registration result and logs a refusal (D4-10)', () => {
    expect(mainSrc).toContain("globalShortcut.register('Alt+Space', toggleMainWindow)");
    expect(mainSrc).toContain('Summon hotkey (Alt+Space) could not be registered');
  });

  it('opens the folder the on-screen logs actually live in (E10-02)', () => {
    const start = mainSrc.indexOf("ipcMain.handle('open-logs-folder'");
    expect(start).toBeGreaterThan(-1);
    const body = mainSrc.slice(start, mainSrc.indexOf('ipcMain.handle', start + 10));
    expect(body).toContain("app.getPath('userData')");
    expect(body).not.toContain("'.aether', 'logs'");
  });

  it('reports the app-log broken state to the viewer (E10-01)', () => {
    expect(mainSrc).toContain('logBroken');
  });

  it('validates open-path against the workspace + attachments roots (N-42)', () => {
    expect(mainSrc).toContain('openPathRoots()');
    const start = mainSrc.indexOf("ipcMain.handle('open-path'");
    const body = mainSrc.slice(start);
    expect(body).toContain('validateOpenPathArg(filePath, { roots');
  });

  it('keeps the engine files behind the guarded ws reconnect module', () => {
    expect(mainSrc).toContain("require('./electron/wsReconnect.cjs')");
    expect(mainSrc).toContain("require('./electron/crashLog.cjs')");
  });
});

describe('engineLifecycle engine log buffering (D4-01)', () => {
  it('buffers engine output and flushes asynchronously — no per-chunk sync append on the main loop', async () => {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'aether-ud-'));
    const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), 'aether-res-'));
    const exe = path.join(resourcesPath, 'engine', 'python', 'bin', 'python3');
    fs.mkdirSync(path.dirname(exe), { recursive: true });
    fs.writeFileSync(exe, '');
    for (const pkg of ['aether_engine', 'aether_common', 'aether_worker']) {
      fs.mkdirSync(path.join(resourcesPath, 'engine', 'app', pkg), { recursive: true });
    }
    fs.writeFileSync(
      path.join(resourcesPath, 'engine', 'payload-manifest.json'),
      validManifest('linux'),
    );
    const appendFileCalls = [];
    const appendFileSyncCalls = [];
    const spyFs = {
      ...fs,
      appendFile: (p, data, cb) => { appendFileCalls.push(p); fs.appendFile(p, data, cb); },
      appendFileSync: (p, data) => { appendFileSyncCalls.push(p); fs.appendFileSync(p, data); },
    };
    let up = false;
    const child = fakeChild(7777);
    const lifecycle = createEngineLifecycle({
      spawn: jest.fn(() => { up = true; return child; }),
      httpGet: jest.fn(async () => {
        if (!up) throw new Error('ECONNREFUSED');
        return 200;
      }),
      fs: spyFs,
      platform: 'linux',
      isPackaged: true,
      resourcesPath,
      userDataPath: userData,
      baseEnv: {},
      log: () => {},
      tuning: {
        healthTimeoutMs: 150,
        healthPollMs: 5,
        restartDelayMs: 5,
        killGraceMs: 25,
        spawnRetryDelayMs: 5,
        maxRestarts: 2,
        engineLogFlushMs: 10,
      },
    });
    const logPath = path.join(userData, 'logs', 'engine.log');
    await expect(lifecycle.start()).resolves.toEqual({ mode: 'spawned' });
    // A burst of engine output lines (the chatty case that used to
    // cost one blocking append per chunk).
    for (let i = 0; i < 6; i += 1) {
      child.stdout.emit('data', Buffer.from(`engine output line ${i}\n`));
    }
    await until(() => fs.existsSync(logPath)
      && fs.readFileSync(logPath, 'utf8').includes('engine output line 5'));
    // The whole burst reached the file through the async path…
    expect(appendFileCalls.filter((p) => p === logPath).length).toBeGreaterThan(0);
    // …and never through a synchronous append.
    expect(appendFileSyncCalls.filter((p) => p === logPath)).toEqual([]);
    // stop() drains the buffer: nothing written before shutdown is lost.
    child.stdout.emit('data', Buffer.from('final line before shutdown\n'));
    await lifecycle.stop();
    expect(fs.readFileSync(logPath, 'utf8')).toContain('final line before shutdown');
  });
});
