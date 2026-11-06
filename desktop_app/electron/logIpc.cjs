'use strict';

// Log IPC actions (Wave 5 split, Annex C row 12).
//
// The Settings → Diagnostics log channels (read-logs,
// get-desktop-diagnostics, clear-logs, set-log-level,
// open-logs-folder) and the append-ui-log sink used to live
// inline in main.cjs. Their bodies live here, behind
// createLogIpc, so Jest can drive them without Electron (the
// appLog.cjs pattern). The registrations stay in main.cjs,
// behind the S3-M3 sender guard and the append-ui-log entry
// validation — the trust boundary is main's wiring; this module
// is only the work behind it.
//
// The log paths and the file inventory live here too: the log
// IPC is their only reader.

const UI_LOG_RATE_LIMIT = 120; // entries…
const UI_LOG_RATE_WINDOW_MS = 1000; // …per rolling window

function createLogIpc(deps = {}) {
  const fs = deps.fs || require('fs');
  const path = deps.path || require('path');
  const os = deps.os || require('os');
  const app = deps.app;
  const platform = deps.platform ?? process.platform;
  const getAppLog = deps.getAppLog;
  const mainLog = deps.mainLog || (() => {});
  const readLogTail = deps.readLogTail;
  const logFileStat = deps.logFileStat;
  const scrubSecrets = deps.scrubSecrets;
  const shellOpenPath = deps.shellOpenPath || (async () => '');

  let uiLogWindowStart = 0;
  let uiLogWindowCount = 0;

  // The paths the log IPC reads. app.log + engine.log live in
  // <userData>/logs (the lifecycle's file); crash-electron.log is
  // the shell crash log from the block at the top of main.cjs,
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

  // append-ui-log rate cap: a renderer stuck in an error loop
  // must not be able to flood the file faster than rotation can
  // bound it. The count increments BEFORE the entry is validated
  // (validation sits in main.cjs, after this check), exactly as
  // the inline limiter did — a flood of invalid entries still
  // spends the budget. Returns false when the entry is dropped.
  function acceptUiLogEntry(nowMs = Date.now()) {
    if (nowMs - uiLogWindowStart >= UI_LOG_RATE_WINDOW_MS) {
      uiLogWindowStart = nowMs;
      uiLogWindowCount = 0;
    }
    uiLogWindowCount += 1;
    if (uiLogWindowCount > UI_LOG_RATE_LIMIT) return false; // dropped silently: logging about logging helps no one
    return true;
  }

  function writeUiLogEntry(value) {
    try { getAppLog().log(value.source, value.level, value.message); } catch { /* never throws by design */ }
  }

  // open-logs-folder: the dir is computed in main.cjs (E10-02:
  // the folder the on-screen logs actually live in —
  // <userData>/logs, where app.log and engine.log are); this
  // action creates it if needed and opens it.
  async function openLogsFolder(dir) {
    fs.mkdirSync(dir, { recursive: true });
    await shellOpenPath(dir);
    return dir;
  }

  // The viewer's data: the tails of both durable logs plus the
  // file inventory (name/size/last-write per source). Raw lines —
  // this is the user's own machine; redaction applies to the
  // EXPORT below.
  function readLogs() {
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
  }

  // Bundle v2, desktop half: everything getDesktopDiagnostics
  // adds to the engine bundle at export time. The three tails
  // pass through the secret-pattern scrubber (the JS mirror of
  // the engine audit redactor's bare-string patterns,
  // appLog.cjs) — the bundle is built to be handed to another
  // person.
  function getDesktopDiagnostics() {
    return {
      app_version: app.getVersion(),
      platform,
      app_log_tail: readLogTail(fs, appLogFilePath(), 500).map((line) => scrubSecrets(line)),
      engine_log_tail: readLogTail(fs, engineLogFilePath(), 500).map((line) => scrubSecrets(line)),
      crash_electron_tail: readLogTail(fs, crashElectronLogPath(), 200).map((line) => scrubSecrets(line)),
      log_inventory: logInventory(),
    };
  }

  // Clear logs (Settings → Diagnostics, behind the panel's
  // two-step confirmation): app.log via the logger (it owns the
  // size cache), engine.log by plain truncate. Since D4-01 the
  // lifecycle buffers engine.log lines for up to one flush
  // window, so lines buffered at the moment of the truncate can
  // still land just after it, and its rotation counter
  // over-counts by the truncated size until the next run
  // (rotation then fires early — the safe direction). Crash logs
  // are NOT cleared — they are the record of last resort.
  function clearLogs() {
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
  }

  // The [main]/[ui] level threshold (Settings → Diagnostics
  // select).
  function setLogLevel(level) {
    const ok = getAppLog().setLevel(level);
    if (ok) mainLog(`Log level set to ${getAppLog().getLevel()} from Settings.`);
    return { ok, level: getAppLog().getLevel() };
  }

  return {
    acceptUiLogEntry,
    writeUiLogEntry,
    openLogsFolder,
    readLogs,
    getDesktopDiagnostics,
    clearLogs,
    setLogLevel,
    appLogFilePath,
    engineLogFilePath,
    crashElectronLogPath,
    logInventory,
  };
}

module.exports = { createLogIpc, UI_LOG_RATE_LIMIT, UI_LOG_RATE_WINDOW_MS };
