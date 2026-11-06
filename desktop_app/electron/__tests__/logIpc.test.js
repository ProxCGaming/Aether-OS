/**
 * Wave 5 (Annex C row 12): the log IPC actions, extracted from
 * main.cjs into electron/logIpc.cjs. Registrations, the S3-M3
 * sender guard and the append-ui-log entry validation stay in
 * main.cjs (pinned there by appLog.test.js / ipcValidation.test.js);
 * this suite drives the work behind them with a fake fs and the
 * real appLog helpers.
 */
const path = require('path');
const os = require('os');
const { createLogIpc, UI_LOG_RATE_LIMIT, UI_LOG_RATE_WINDOW_MS } = require('../logIpc.cjs');
const { readLogTail, logFileStat, scrubSecrets } = require('../appLog.cjs');

const USER_DATA = '/userData';
const APP_LOG = path.join(USER_DATA, 'logs', 'app.log');
const ENGINE_LOG = path.join(USER_DATA, 'logs', 'engine.log');
const CRASH_LOG = path.join(os.homedir(), '.aether', 'logs', 'crash-electron.log');

function fakeFs(initial = {}) {
  const files = { ...initial };
  const dirs = [];
  return {
    files,
    dirs,
    readFileSync(p) {
      if (!(p in files)) throw new Error('ENOENT');
      return files[p];
    },
    writeFileSync(p, data) {
      files[p] = data;
    },
    mkdirSync(p) {
      dirs.push(p);
    },
    statSync(p) {
      if (!(p in files)) throw new Error('ENOENT');
      return { size: Buffer.byteLength(files[p]), mtimeMs: 1700000000000 };
    },
  };
}

function makeLogIpc({ fs = fakeFs(), appLogOverrides = {}, failEngineClear = false } = {}) {
  const calls = { log: [], mainLog: [], shellOpenPath: [], cleared: 0 };
  const appLog = {
    filePath: () => APP_LOG,
    getLevel: () => 'INFO',
    isBroken: () => false,
    clear: () => {
      calls.cleared += 1;
      return true;
    },
    setLevel: (level) => level === 'DEBUG',
    log: (source, level, message) => calls.log.push([source, level, message]),
    ...appLogOverrides,
  };
  if (failEngineClear) {
    fs.writeFileSync = () => {
      throw new Error('EPERM');
    };
  }
  const ipc = createLogIpc({
    fs,
    path,
    os,
    app: {
      getPath: (name) => (name === 'userData' ? USER_DATA : ''),
      getVersion: () => '9.9.9',
    },
    platform: 'testix',
    getAppLog: () => appLog,
    mainLog: (...args) => calls.mainLog.push(args.join(' ')),
    readLogTail,
    logFileStat,
    scrubSecrets,
    shellOpenPath: async (dir) => {
      calls.shellOpenPath.push(dir);
      return '';
    },
  });
  return { ipc, calls, fs };
}

describe('createLogIpc', () => {
  it('readLogs returns both tails, the inventory, level and sink health', () => {
    const fs = fakeFs({ [APP_LOG]: 'a1\na2\n', [ENGINE_LOG]: 'e1\n' });
    const { ipc } = makeLogIpc({ fs });
    const out = ipc.readLogs();
    expect(out.appTail).toEqual(['a1', 'a2']);
    expect(out.engineTail).toEqual(['e1']);
    // crash-electron.log is absent in the fake fs, so the
    // inventory filters it out.
    expect(out.inventory.map((entry) => entry.name)).toEqual(['app.log', 'engine.log']);
    expect(out.level).toBe('INFO');
    expect(out.logBroken).toBe(false);
  });

  it('getDesktopDiagnostics scrubs secrets from every tail', () => {
    const secretLine = 'key is sk-abcdefgh12345678 ok';
    const fs = fakeFs({ [APP_LOG]: `${secretLine}\n`, [ENGINE_LOG]: 'clean\n', [CRASH_LOG]: 'crash\n' });
    const { ipc } = makeLogIpc({ fs });
    const out = ipc.getDesktopDiagnostics();
    expect(out.app_version).toBe('9.9.9');
    expect(out.platform).toBe('testix');
    expect(out.app_log_tail).toEqual(['key is [REDACTED] ok']);
    expect(out.engine_log_tail).toEqual(['clean']);
    expect(out.crash_electron_tail).toEqual(['crash']);
    expect(out.log_inventory).toHaveLength(3);
  });

  it('clearLogs clears via the logger and truncates engine.log', () => {
    const fs = fakeFs({ [ENGINE_LOG]: 'old lines\n' });
    const { ipc, calls } = makeLogIpc({ fs });
    expect(ipc.clearLogs()).toEqual({ ok: true });
    expect(calls.cleared).toBe(1);
    expect(fs.files[ENGINE_LOG]).toBe('');
  });

  it('clearLogs reports failure when the engine log cannot be truncated', () => {
    const { ipc } = makeLogIpc({ failEngineClear: true });
    const out = ipc.clearLogs();
    expect(out.ok).toBe(false);
    expect(out.error).toContain('could not be cleared');
  });

  it('setLogLevel mirrors the logger result and announces a successful change', () => {
    let level = 'INFO';
    const { ipc, calls } = makeLogIpc({
      appLogOverrides: {
        getLevel: () => level,
        setLevel: (next) => {
          if (next !== 'DEBUG') return false;
          level = 'DEBUG';
          return true;
        },
      },
    });
    expect(ipc.setLogLevel('DEBUG')).toEqual({ ok: true, level: 'DEBUG' });
    expect(calls.mainLog.join('\n')).toContain('Log level set to DEBUG from Settings.');
    expect(ipc.setLogLevel('SHOUTY')).toEqual({ ok: false, level: 'DEBUG' });
  });

  it('openLogsFolder creates and opens the folder main computed (E10-02)', async () => {
    const { ipc, calls, fs } = makeLogIpc({});
    const dir = path.join(USER_DATA, 'logs');
    await expect(ipc.openLogsFolder(dir)).resolves.toBe(dir);
    expect(fs.dirs).toContain(dir);
    expect(calls.shellOpenPath).toEqual([dir]);
  });

  it('acceptUiLogEntry caps a rolling window, counting before validation', () => {
    const { ipc } = makeLogIpc({});
    const t0 = 1000000;
    for (let i = 0; i < UI_LOG_RATE_LIMIT; i += 1) {
      expect(ipc.acceptUiLogEntry(t0 + i)).toBe(true);
    }
    expect(ipc.acceptUiLogEntry(t0 + UI_LOG_RATE_LIMIT)).toBe(false);
    // The window rolls: after the window elapses, entries pass again.
    expect(ipc.acceptUiLogEntry(t0 + UI_LOG_RATE_WINDOW_MS + 1)).toBe(true);
  });

  it('writeUiLogEntry writes through the app logger', () => {
    const { ipc, calls } = makeLogIpc({});
    ipc.writeUiLogEntry({ source: 'ui', level: 'WARN', message: 'socket closed' });
    expect(calls.log).toEqual([['ui', 'WARN', 'socket closed']]);
  });
});
