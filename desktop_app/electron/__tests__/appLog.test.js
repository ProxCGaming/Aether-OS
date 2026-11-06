/**
 * Unified logging — electron/appLog.cjs, the shell log core.
 *
 * appLog owns the decisions: line format, level threshold,
 * rotation, never-throw. main.cjs is wiring only and cannot be
 * required in Jest, so — like engineLifecycle.test.js — the
 * main-process half is pinned by source-contract assertions at
 * the bottom. The logger itself is driven against a real fs in a
 * temp directory: rotation is a filesystem behaviour, and faking
 * it would test the fake.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  createAppLog,
  scrubSecrets,
  normalizeLevel,
  readLogTail,
  logFileStat,
  MAX_LOG_BYTES,
  KEEP_ARCHIVES,
} = require('../appLog.cjs');

const appRoot = path.join(__dirname, '..', '..');
const mainSrc = fs.readFileSync(path.join(appRoot, 'main.cjs'), 'utf8');
const preloadSrc = fs.readFileSync(path.join(appRoot, 'preload.cjs'), 'utf8');

const FIXED_NOW = Date.parse('2026-11-05T12:00:00.000Z');
const STAMP = '2026-11-05T12:00:00.000Z';

function makeLog(overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aether-applog-'));
  const userDataPath = path.join(dir, 'userData');
  const logger = createAppLog({
    fs,
    userDataPath,
    now: () => FIXED_NOW,
    ...overrides,
  });
  return { dir, userDataPath, logger, logPath: path.join(userDataPath, 'logs', 'app.log') };
}

function readLines(file) {
  return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean);
}

describe('createAppLog line format', () => {
  it('writes ISO [source] LEVEL message lines to <userData>/logs/app.log', () => {
    const { logger, logPath } = makeLog();
    logger.info('main', 'app ready');
    logger.warn('ui', 'socket closed');
    logger.error('main', 'boom');
    expect(readLines(logPath)).toEqual([
      `${STAMP} [main] INFO app ready`,
      `${STAMP} [ui] WARN socket closed`,
      `${STAMP} [main] ERROR boom`,
    ]);
  });

  it('prefixes every physical line of a multi-line message', () => {
    const { logger, logPath } = makeLog();
    logger.error('main', 'first\nsecond');
    expect(readLines(logPath)).toEqual([
      `${STAMP} [main] ERROR first`,
      `${STAMP} [main] ERROR second`,
    ]);
  });

  it('writes a launch marker naming version, platform and pid', () => {
    const { logger, logPath } = makeLog();
    logger.launchMarker({ version: '0.3.5', platform: 'win32', pid: 4242 });
    expect(readLines(logPath)).toEqual([
      `${STAMP} [main] INFO === launch: Aether-OS 0.3.5 (win32, pid 4242) ===`,
    ]);
  });

  it('creates the logs directory lazily', () => {
    const { logger, userDataPath } = makeLog();
    expect(fs.existsSync(path.join(userDataPath, 'logs'))).toBe(false);
    logger.info('main', 'first line');
    expect(fs.existsSync(path.join(userDataPath, 'logs', 'app.log'))).toBe(true);
  });
});

describe('createAppLog level threshold', () => {
  it('drops lines below the threshold and honours setLevel', () => {
    const { logger, logPath } = makeLog();
    logger.debug('ui', 'hidden at INFO');
    logger.info('ui', 'shown');
    expect(readLines(logPath)).toEqual([`${STAMP} [ui] INFO shown`]);

    expect(logger.setLevel('DEBUG')).toBe(true);
    expect(logger.getLevel()).toBe('DEBUG');
    logger.debug('ui', 'now visible');
    expect(readLines(logPath)).toContain(`${STAMP} [ui] DEBUG now visible`);

    expect(logger.setLevel('ERROR')).toBe(true);
    logger.warn('ui', 'hidden at ERROR');
    expect(readLines(logPath).some((l) => l.includes('hidden at ERROR'))).toBe(false);
  });

  it('refuses an unknown level and keeps the current one', () => {
    const { logger } = makeLog();
    expect(logger.setLevel('SHOUTY')).toBe(false);
    expect(logger.getLevel()).toBe('INFO');
    expect(normalizeLevel('warn')).toBe('WARN');
    expect(normalizeLevel('nope')).toBe(null);
    expect(normalizeLevel(42)).toBe(null);
  });
});

describe('createAppLog rotation', () => {
  it('rotates at the byte cap and keeps at most the configured archives', () => {
    const { logger, logPath } = makeLog({ maxBytes: 120, keepArchives: 2 });
    for (let i = 0; i < 40; i += 1) {
      logger.info('main', `line number ${i} with some padding text`);
    }
    expect(fs.existsSync(`${logPath}.1`)).toBe(true);
    expect(fs.existsSync(`${logPath}.2`)).toBe(true);
    expect(fs.existsSync(`${logPath}.3`)).toBe(false);
    // The newest line is always in the live file.
    expect(fs.readFileSync(logPath, 'utf8')).toContain('line number 39');
  });

  it('exposes the production rotation constants from the spec (5 MB x 3)', () => {
    expect(MAX_LOG_BYTES).toBe(5 * 1024 * 1024);
    expect(KEEP_ARCHIVES).toBe(3);
  });
});

describe('createAppLog never throws', () => {
  it('disables itself on write failure instead of throwing', () => {
    const failingFs = {
      ...fs,
      mkdirSync: () => { throw new Error('disk gone'); },
    };
    const logger = createAppLog({ fs: failingFs, userDataPath: '/nowhere', now: () => FIXED_NOW });
    expect(() => logger.info('main', 'into the void')).not.toThrow();
    expect(logger.isBroken()).toBe(true);
    expect(() => logger.launchMarker({ version: '1', platform: 'x', pid: 1 })).not.toThrow();
  });

  it('is inert without a userData path', () => {
    const logger = createAppLog({ fs, userDataPath: null });
    expect(logger.filePath()).toBe(null);
    expect(() => logger.info('main', 'nothing')).not.toThrow();
  });
});

describe('createAppLog clear', () => {
  it('truncates the live file, drops archives and leaves a marker', () => {
    const { logger, logPath } = makeLog({ maxBytes: 80, keepArchives: 2 });
    for (let i = 0; i < 10; i += 1) logger.info('main', `filler line ${i} xxxxxxxx`);
    expect(fs.existsSync(`${logPath}.1`)).toBe(true);
    expect(logger.clear()).toBe(true);
    expect(fs.existsSync(`${logPath}.1`)).toBe(false);
    const lines = readLines(logPath);
    expect(lines).toEqual([`${STAMP} [main] INFO logs cleared from Settings`]);
  });
});

describe('scrubSecrets', () => {
  it('redacts the audit redactor key shapes', () => {
    expect(scrubSecrets('key sk-abcdef1234567890 here')).toBe('key [REDACTED] here');
    expect(scrubSecrets('AKIAIOSFODNN7EXAMPLE')).toBe('[REDACTED]');
    expect(scrubSecrets('ghp_abcdefghijklmnopqrstuvwxyz')).toBe('[REDACTED]');
    expect(scrubSecrets('-----BEGIN RSA PRIVATE KEY-----')).toBe('[REDACTED]');
    expect(scrubSecrets('ordinary log text')).toBe('ordinary log text');
  });
});

describe('readLogTail / logFileStat helpers', () => {
  it('returns the last N lines and [] for a missing file', () => {
    const { logger, logPath, userDataPath } = makeLog();
    for (let i = 0; i < 5; i += 1) logger.info('main', `line ${i}`);
    expect(readLogTail(fs, logPath, 2)).toEqual([
      `${STAMP} [main] INFO line 3`,
      `${STAMP} [main] INFO line 4`,
    ]);
    expect(readLogTail(fs, path.join(userDataPath, 'logs', 'nope.log'), 5)).toEqual([]);
  });

  it('stats a file for the inventory and returns null when missing', () => {
    const { logger, logPath } = makeLog();
    logger.info('main', 'x');
    const stat = logFileStat(fs, logPath, 'app.log');
    expect(stat.name).toBe('app.log');
    expect(stat.size).toBeGreaterThan(0);
    expect(typeof stat.mtime).toBe('string');
    expect(logFileStat(fs, '/no/such/file.log', 'ghost.log')).toBe(null);
  });
});

describe('main.cjs / preload.cjs wiring (source contract)', () => {
  it('main creates the app logger and routes through it', () => {
    expect(mainSrc).toContain("require('./electron/appLog.cjs')");
    expect(mainSrc).toContain('createAppLog');
    expect(mainSrc).toContain('launchMarker');
  });

  it('main exposes the logging IPC channels behind the sender guard', () => {
    for (const channel of ['append-ui-log', 'read-logs', 'get-desktop-diagnostics', 'clear-logs', 'set-log-level']) {
      expect(mainSrc).toContain(`'${channel}'`);
      expect(mainSrc).toContain(`isTrustedIpcSender(event, '${channel}')`);
    }
    expect(mainSrc).toContain('validateUiLogEntry');
  });

  it('preload exposes the renderer logging surface', () => {
    expect(preloadSrc).toContain('appendUiLog');
    expect(preloadSrc).toContain('readLogs');
    expect(preloadSrc).toContain('getDesktopDiagnostics');
    expect(preloadSrc).toContain('clearLogs');
    expect(preloadSrc).toContain('setLogLevel');
  });
});
