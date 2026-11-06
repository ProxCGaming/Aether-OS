// Unified logging (5 Nov 2026) — the renderer's half of app.log.
//
// The desktop shell records [main] lines itself; everything the
// renderer knows — window errors, unhandled rejections, the engine
// socket's open/close, and the message TYPES crossing the socket
// facade — reaches the same file through the append-ui-log IPC
// (window.electronAPI.appendUiLog, validated in main: source is
// always 'ui', level from a fixed set, message truncated).
//
// Two disciplines, both deliberate:
//   - Payloads are never logged. logMessageType records the TYPE
//     of an allowlisted message and nothing else — the same rule
//     the engine's WS tracing follows.
//   - Fully inert without the bridge. In a browser/dev context
//     window.electronAPI.appendUiLog is absent and every call here
//     is a no-op; logging must never break the app it describes.

export const MESSAGE_TYPE_ALLOWLIST_PREFIXES = [
  'MODEL_',
  'TASK_',
  'PROVIDER_',
  'DIAGNOSTICS_',
  'CONFIG_',
  'ENVIRONMENT_',
];

export function isAllowlistedMessageType(type) {
  if (typeof type !== 'string' || type.length === 0) return false;
  return MESSAGE_TYPE_ALLOWLIST_PREFIXES.some((prefix) => type.startsWith(prefix));
}

// The type of a serialized envelope, or null when the packet is
// not a JSON object with a string type. Never throws — packets on
// this path include engine stream traffic in shapes this helper
// does not need to understand.
export function messageTypeOf(data) {
  if (typeof data !== 'string') return null;
  try {
    const parsed = JSON.parse(data);
    return parsed && typeof parsed.type === 'string' ? parsed.type : null;
  } catch {
    return null;
  }
}

// A repeat of the same window error inside this window is one
// fault reported twice (a throwing render loop can fire it per
// frame), so error forwarding dedupes on message + stack head —
// the SocketContext packet-dedup idiom, applied to errors.
const ERROR_DEDUP_WINDOW_MS = 1000;
const MAX_ERROR_CHARS = 1800;

// The two socket lifecycle kinds that exist (E10-05): the IPC
// transport carries open/close status only — there is no error
// status for a third kind to be fed from, and a former 'error'
// entry presented as a live record path while no caller could
// ever emit it. The practical case is still captured: an error is
// followed by a close.
const WS_EVENT_LEVELS = { open: 'INFO', close: 'WARN' };

export function createUiLogger(send) {
  const emit = (level, message) => {
    if (typeof send !== 'function') return;
    try {
      send({ source: 'ui', level, message });
    } catch {
      // Logging never breaks the app.
    }
  };

  const seenErrors = new Map();

  function reportError(message, stack) {
    const text = String(message || 'Unknown error');
    const stackText = stack ? String(stack) : '';
    const key = `${text}|${stackText.split('\n')[0] || ''}`;
    const now = Date.now();
    for (const [seenKey, seenAt] of seenErrors) {
      if (now - seenAt >= ERROR_DEDUP_WINDOW_MS) seenErrors.delete(seenKey);
    }
    if (seenErrors.has(key)) return;
    seenErrors.set(key, now);
    const body = stackText ? `${text}\n${stackText}` : text;
    emit('ERROR', body.slice(0, MAX_ERROR_CHARS));
  }

  return {
    // Facade socket lifecycle: open is routine (INFO), a close is
    // the event every "engine closed" mystery needs on record
    // (WARN). Those are the only two kinds the transport reports.
    logWsEvent(kind, detail) {
      const level = WS_EVENT_LEVELS[kind];
      if (!level) return;
      const suffix = detail ? `: ${String(detail).slice(0, 300)}` : '';
      emit(level, `engine socket ${kind}${suffix}`);
    },

    // Message-type tracing for the allowlist only, at DEBUG: the
    // shell's default threshold is INFO, so the trace stays silent
    // until the user raises the level in Settings → Diagnostics —
    // the same opt-in the engine side uses for non-allowlist types.
    logMessageType(direction, type) {
      if (direction !== 'in' && direction !== 'out') return;
      if (!isAllowlistedMessageType(type)) return;
      emit('DEBUG', `ws message ${direction}: ${type}`);
    },

    reportError,
  };
}

// Attach window error forwarding (window.onerror via the 'error'
// event, plus unhandledrejection) to a logger — or to a fresh one
// built on `send`. Returns { logger, uninstall }; uninstall removes
// exactly the listeners this call added.
export function installUiLogging(sendOrLogger) {
  const logger = sendOrLogger && typeof sendOrLogger.reportError === 'function'
    ? sendOrLogger
    : createUiLogger(sendOrLogger);
  if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') {
    return { logger, uninstall: () => {} };
  }

  const onError = (event) => {
    const err = event && event.error;
    const message = (err && err.message) || (event && event.message) || 'Unknown window error';
    logger.reportError(message, err && err.stack);
  };
  const onRejection = (event) => {
    const reason = event && event.reason;
    if (reason instanceof Error) {
      logger.reportError(reason.message, reason.stack);
    } else {
      logger.reportError(`Unhandled rejection: ${String(reason)}`, null);
    }
  };

  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);
  return {
    logger,
    uninstall: () => {
      window.removeEventListener('error', onError);
      window.removeEventListener('unhandledrejection', onRejection);
    },
  };
}
