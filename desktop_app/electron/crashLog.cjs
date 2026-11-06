'use strict';

// Shell crash log (Epic 10; rotation added Wave 3, D4-01 remainder).
//
// Uncaught exceptions and unhandled rejections in the main process
// land in ~/.aether/logs/crash-electron.log, next to the engine's
// own crash log, so the diagnostics view/export has the shell's
// side of a crash. The sink used to be a bare appendFileSync block
// at the top of main.cjs with no size bound at all: a crash-looping
// install could grow the file forever. The writer now rotates like
// the other shell logs (appLog.cjs, engineLifecycle.cjs): at
// MAX_BYTES the file rolls to .1, older archives shift up, at most
// KEEP_ARCHIVES survive.
//
// The discipline that must never change: this writer runs when
// everything else has already failed, so it NEVER throws — a
// logging failure inside the crash handler would become the crash.

const path = require('path');

const CRASH_LOG_MAX_BYTES = 5 * 1024 * 1024;
const CRASH_LOG_KEEP_ARCHIVES = 3;

function createCrashLogWriter(deps = {}) {
  const fs = deps.fs || require('fs');
  const crashDir = deps.crashDir;
  const maxBytes = deps.maxBytes ?? CRASH_LOG_MAX_BYTES;
  const keepArchives = deps.keepArchives ?? CRASH_LOG_KEEP_ARCHIVES;
  const crashPath = crashDir ? path.join(crashDir, 'crash-electron.log') : null;

  let ready = false;
  let currentSize = null; // lazily stat'ed on first write

  function archivePath(index) {
    return `${crashPath}.${index}`;
  }

  function rotate() {
    if (keepArchives < 1) {
      fs.rmSync(crashPath, { force: true });
      return;
    }
    try { fs.rmSync(archivePath(keepArchives), { force: true }); } catch { /* may not exist */ }
    for (let i = keepArchives - 1; i >= 1; i -= 1) {
      try { fs.renameSync(archivePath(i), archivePath(i + 1)); } catch { /* may not exist */ }
    }
    fs.renameSync(crashPath, archivePath(1));
  }

  function writeCrash(kind, err) {
    if (!crashPath) return;
    try {
      if (!ready) {
        fs.mkdirSync(crashDir, { recursive: true });
        ready = true;
      }
      if (currentSize === null) {
        try {
          currentSize = fs.statSync(crashPath).size;
        } catch {
          currentSize = 0;
        }
      }
      const entry = `\n=== ${kind} ===\n${(err && err.stack) || err}\n`;
      const bytes = Buffer.byteLength(entry, 'utf8');
      if (currentSize > 0 && currentSize + bytes > maxBytes) {
        rotate();
        currentSize = 0;
      }
      fs.appendFileSync(crashPath, entry);
      currentSize += bytes;
    } catch { /* never crash on logging */ }
  }

  return writeCrash;
}

module.exports = {
  createCrashLogWriter,
  CRASH_LOG_MAX_BYTES,
  CRASH_LOG_KEEP_ARCHIVES,
};
