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
const path = require('path');
const { EventEmitter } = require('events');

const {
  createEngineLifecycle,
  MAX_RESTARTS,
} = require('../engineLifecycle.cjs');

const appRoot = path.join(__dirname, '..', '..');
const mainSrc = fs.readFileSync(path.join(appRoot, 'main.cjs'), 'utf8');

const RESOURCES = path.join(path.sep, 'fake', 'resources');
const USER_DATA = path.join(path.sep, 'fake', 'userData');
const PAYLOAD_EXE = path.join(RESOURCES, 'engine', 'python', 'bin', 'python3');
const PAYLOAD_APP = path.join(RESOURCES, 'engine', 'app');
const RUN_DIR = path.join(USER_DATA, 'engine-run');

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
  };
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
      if (overrides.engineBoots !== false) state.healthUp = true;
      return child;
    }),
    httpGet: jest.fn(async () => {
      if (!state.healthUp) throw new Error('ECONNREFUSED');
      return 200;
    }),
    fs: {
      existsSync: (p) => state.existing.has(p),
      mkdirSync: (p, opts) => { state.mkdirCalls.push({ p, opts }); },
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
      killGraceMs: 25,
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
    const { lifecycle, state } = makeLifecycle({
      existing: [PAYLOAD_EXE, PAYLOAD_APP],
      maxRestarts: 2,
    });
    await lifecycle.start();
    expect(state.children).toHaveLength(1);
    state.children[0].die(1);
    await until(() => state.children.length === 2);
    state.children[1].die(1);
    await until(() => state.children.length === 3);
    state.children[2].die(1);
    await tick(60);
    expect(state.children).toHaveLength(3); // 1 initial + 2 restarts, no more
    expect(lifecycle.getMode()).toBe('none');
    expect(state.logs.join('\n')).toMatch(/restart bound reached/);
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
