/**
 * D4-01 (remainder) — the shell crash log's rotation.
 *
 * electron/crashLog.cjs owns the crash-electron.log sink that used
 * to live as an unrotated appendFileSync block at the top of
 * main.cjs: every uncaught exception appended forever, with no cap,
 * into the file the diagnostics export tails. The writer now
 * rotates like the other shell logs (size cap + bounded archives)
 * and keeps the never-crash-on-logging discipline — the crash
 * handler is the last code standing when everything else failed.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const { createCrashLogWriter, CRASH_LOG_MAX_BYTES, CRASH_LOG_KEEP_ARCHIVES } = require('../crashLog.cjs');

function makeWriter(overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aether-crashlog-'));
  const crashPath = path.join(dir, 'crash-electron.log');
  const writer = createCrashLogWriter({ fs, crashDir: dir, ...overrides });
  return { dir, crashPath, writer };
}

describe('crashLog writer', () => {
  it('creates the directory and appends stamped crash entries', () => {
    const { crashPath, writer } = makeWriter();
    const err = new Error('boom');
    writer('UNCAUGHT', err);
    const content = fs.readFileSync(crashPath, 'utf8');
    expect(content).toContain('=== UNCAUGHT ===');
    expect(content).toContain('Error: boom');
  });

  it('rotates at the cap and keeps only the configured archives', () => {
    const { crashPath, writer } = makeWriter({ maxBytes: 300, keepArchives: 2 });
    for (let i = 0; i < 12; i += 1) {
      writer('UNHANDLED_REJECTION', new Error(`failure number ${i} with padding`));
    }
    expect(fs.existsSync(`${crashPath}.1`)).toBe(true);
    expect(fs.existsSync(`${crashPath}.2`)).toBe(true);
    expect(fs.existsSync(`${crashPath}.3`)).toBe(false);
    expect(fs.readFileSync(crashPath, 'utf8')).toContain('failure number 11');
  });

  it('ships bounded production constants (5 MB x 3, like the other shell logs)', () => {
    expect(CRASH_LOG_MAX_BYTES).toBe(5 * 1024 * 1024);
    expect(CRASH_LOG_KEEP_ARCHIVES).toBe(3);
  });

  it('never throws when the filesystem fails — the crash handler must not crash', () => {
    const failingFs = {
      ...fs,
      mkdirSync: () => { throw new Error('disk gone'); },
      appendFileSync: () => { throw new Error('disk gone'); },
    };
    const writer = createCrashLogWriter({ fs: failingFs, crashDir: '/nowhere' });
    expect(() => writer('UNCAUGHT', new Error('boom'))).not.toThrow();
  });

  it('records non-Error values verbatim', () => {
    const { crashPath, writer } = makeWriter();
    writer('UNHANDLED_REJECTION', 'a string rejection');
    expect(fs.readFileSync(crashPath, 'utf8')).toContain('a string rejection');
  });
});
