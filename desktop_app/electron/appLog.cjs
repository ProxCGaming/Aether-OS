'use strict';

// Unified logging (5 Nov 2026) — the shell-side log core.
//
// Before this module the desktop app had no unified log at all:
// the Electron main process wrote to a console nobody can see in a
// packaged GUI app, the renderer wrote nowhere, and the only
// durable file was the engine lifecycle's engine.log (see
// engineLifecycle.cjs — this module deliberately mirrors its sink
// discipline: lazy directory creation, a write failure is swallowed
// rather than thrown, and logging must NEVER throw into app
// logic). One deliberate divergence since E10-01: a failure here
// no longer disables file logging for the session — the next line
// retries (see appendLine).
//
// app.log lives next to engine.log in <userData>/logs/ and carries
// every [main] and [ui] line in one format:
//
//   2026-11-05T12:00:00.000Z [main] INFO message text
//
// Rotation: at MAX_LOG_BYTES the file rolls to app.log.1, older
// archives shift up, and at most KEEP_ARCHIVES archives survive —
// the same bounded-disk rule the engine's audit log follows.
//
// Pure logic with injected fs, so Jest drives it without Electron
// (the engineLifecycle.test.js pattern); main.cjs is wiring only.

const path = require('path');

const LEVELS = { DEBUG: 10, INFO: 20, WARN: 30, ERROR: 40 };
const LEVEL_NAMES = Object.keys(LEVELS);
const MAX_LOG_BYTES = 5 * 1024 * 1024;
const KEEP_ARCHIVES = 3;

// Bare-string secret patterns, mirrored from the engine audit
// redactor (aether_engine/audit.py, _SECRET_PATTERNS) so a log line
// that somehow contains a key-shaped value is scrubbed before it is
// ever exported in a diagnostics bundle. Keep the two lists in step.
const SECRET_PATTERNS = [
  /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
  /sk-[A-Za-z0-9_-]{8,}/g,
  // E8-12: Google API keys (AIza + 35) — the default provider's shape.
  /AIza[0-9A-Za-z_-]{35}/g,
  /(?:AKIA|ASIA)[0-9A-Z]{16}/g,
  /gh[pousr]_[A-Za-z0-9]{20,}/g,
  /github_pat_[A-Za-z0-9_]{20,}/g,
  /xox[baprs]-[A-Za-z0-9-]{8,}/g,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/g,
];
// E8-12: URL userinfo credentials — only the password segment goes.
const URL_CREDENTIAL_PATTERN = /\b([a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^\s/@:]+:)[^\s/@]+@/g;
const REDACTED = '[REDACTED]';

function scrubSecrets(text) {
  let out = String(text);
  out = out.replace(URL_CREDENTIAL_PATTERN, `$1${REDACTED}@`);
  for (const pattern of SECRET_PATTERNS) {
    out = out.replace(pattern, REDACTED);
  }
  return out;
}

function normalizeLevel(level) {
  if (typeof level !== 'string') return null;
  const upper = level.toUpperCase();
  return Object.prototype.hasOwnProperty.call(LEVELS, upper) ? upper : null;
}

// Read the last `maxLines` lines of a file; [] when it is missing or
// unreadable. Used by the read-logs / diagnostics IPC in main.cjs.
function readLogTail(fs, filePath, maxLines) {
  try {
    const text = fs.readFileSync(filePath, 'utf8');
    const lines = text.split('\n').filter((line) => line.length > 0);
    return lines.slice(-maxLines);
  } catch {
    return [];
  }
}

// { name, size, mtime } for one file, or null when it does not
// exist. mtime is an ISO string so the renderer can format it.
function logFileStat(fs, filePath, name) {
  try {
    const stat = fs.statSync(filePath);
    return {
      name,
      size: stat.size,
      mtime: new Date(stat.mtimeMs).toISOString(),
    };
  } catch {
    return null;
  }
}

function createAppLog(deps = {}) {
  const fs = deps.fs || require('fs');
  const userDataPath = deps.userDataPath || null;
  const now = deps.now || Date.now;
  const maxBytes = deps.maxBytes ?? MAX_LOG_BYTES;
  const keepArchives = deps.keepArchives ?? KEEP_ARCHIVES;

  const logFilePath = userDataPath
    ? path.join(userDataPath, 'logs', 'app.log')
    : null;

  let levelThreshold = LEVELS[normalizeLevel(deps.level) || 'INFO'];
  let ready = false;
  let broken = false;
  let currentSize = null; // lazily stat'ed on first write

  function archivePath(index) {
    return `${logFilePath}.${index}`;
  }

  function rotate() {
    // Drop the oldest archive, shift the rest up, current -> .1.
    // Any failure here is swallowed by the caller's try/catch and
    // disables file logging rather than breaking the app.
    if (keepArchives < 1) {
      fs.rmSync(logFilePath, { force: true });
      return;
    }
    try { fs.rmSync(archivePath(keepArchives), { force: true }); } catch { /* may not exist */ }
    for (let i = keepArchives - 1; i >= 1; i -= 1) {
      try { fs.renameSync(archivePath(i), archivePath(i + 1)); } catch { /* may not exist */ }
    }
    fs.renameSync(logFilePath, archivePath(1));
  }

  // E10-01: consecutive write failures before the sink reports
  // itself broken. One transient failure (a locked file during an
  // AV scan of a fresh install, a rotation rename racing another
  // process) must neither disable logging NOR be reported as an
  // outage — the next line simply retries. A directory that cannot
  // even be created is broken immediately: no retry can fix it.
  const MAX_CONSECUTIVE_WRITE_FAILURES = 3;
  let consecutiveWriteFailures = 0;

  function appendLine(line) {
    if (!logFilePath) return;
    try {
      if (!ready) {
        fs.mkdirSync(path.dirname(logFilePath), { recursive: true });
        ready = true;
      }
      if (currentSize === null) {
        try {
          currentSize = fs.statSync(logFilePath).size;
        } catch {
          currentSize = 0;
        }
      }
      const bytes = Buffer.byteLength(line, 'utf8');
      if (currentSize > 0 && currentSize + bytes > maxBytes) {
        rotate();
        currentSize = 0;
      }
      fs.appendFileSync(logFilePath, line);
      currentSize += bytes;
      consecutiveWriteFailures = 0;
      broken = false;
    } catch {
      // The failure costs this one line. The next line re-stats
      // the file (its size may have changed under us) and tries
      // again; only a SUSTAINED failure is reported broken (the
      // state main.cjs surfaces to the Logs panel via read-logs).
      consecutiveWriteFailures += 1;
      broken = !ready || consecutiveWriteFailures >= MAX_CONSECUTIVE_WRITE_FAILURES;
      currentSize = null; // force a re-stat on the next attempt
    }
  }

  // log(source, level, message): one logical message can span
  // several physical lines; each line gets the full prefix, like
  // the engine.log sink in engineLifecycle.cjs.
  function log(source, level, message) {
    const name = normalizeLevel(level) || 'INFO';
    if (LEVELS[name] < levelThreshold) return;
    const stamp = new Date(now()).toISOString();
    const body = String(message)
      .split('\n')
      .map((line) => `${stamp} [${source}] ${name} ${line}`)
      .join('\n');
    appendLine(`${body}\n`);
  }

  // The launch marker: one line at every app start naming the
  // version, platform and pid, so a shared log file separates
  // sessions cleanly.
  function launchMarker({ version, platform, pid } = {}) {
    log('main', 'INFO', `=== launch: Aether-OS ${version || 'unknown'} (${platform || 'unknown'}, pid ${pid ?? '?'}) ===`);
  }

  function setLevel(level) {
    const name = normalizeLevel(level);
    if (!name) return false;
    levelThreshold = LEVELS[name];
    return true;
  }

  function getLevel() {
    return LEVEL_NAMES.find((name) => LEVELS[name] === levelThreshold) || 'INFO';
  }

  // Clear logs (Settings → Diagnostics): truncate app.log and drop
  // its archives, then leave a marker line so the file explains
  // itself. engine.log is main.cjs's own truncate — the lifecycle
  // keeps no size cache, so a plain truncate is safe there.
  function clear() {
    if (!logFilePath) return false;
    try {
      fs.mkdirSync(path.dirname(logFilePath), { recursive: true });
      for (let i = 1; i <= keepArchives; i += 1) {
        try { fs.rmSync(archivePath(i), { force: true }); } catch { /* may not exist */ }
      }
      fs.writeFileSync(logFilePath, '');
      currentSize = 0;
      ready = true;
      broken = false;
      log('main', 'INFO', 'logs cleared from Settings');
      return true;
    } catch {
      broken = true;
      return false;
    }
  }

  return {
    log,
    info: (source, message) => log(source, 'INFO', message),
    warn: (source, message) => log(source, 'WARN', message),
    error: (source, message) => log(source, 'ERROR', message),
    debug: (source, message) => log(source, 'DEBUG', message),
    launchMarker,
    setLevel,
    getLevel,
    clear,
    filePath: () => logFilePath,
    isBroken: () => broken,
  };
}

module.exports = {
  createAppLog,
  scrubSecrets,
  normalizeLevel,
  readLogTail,
  logFileStat,
  LEVELS,
  LEVEL_NAMES,
  MAX_LOG_BYTES,
  KEEP_ARCHIVES,
};
