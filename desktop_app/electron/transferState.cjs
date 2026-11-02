/**
 * Floating-window transfer state (S3-M7).
 *
 * When the chat pops out into the floating window, the full chat
 * history plus the in-progress draft input are handed over through
 * this store. It lives ONLY in main-process memory: it used to be
 * persisted in the renderer's localStorage under
 * 'floating_transfer_state', where any script running in the
 * renderer could read the entire chat history from web storage.
 *
 * Lifetime: set by the main window on detach, read by the floating
 * window on load (a renderer refresh re-reads it, so the floating
 * window keeps its context), updated by the floating window as its
 * messages change, and cleared by main.cjs when the floating window
 * closes for real. Nothing here ever touches disk or web storage.
 *
 * Pure in-memory logic so it is unit-testable outside Electron,
 * like windowState.cjs / notify.cjs.
 */

function normalizeTransferState(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  return {
    messages: Array.isArray(raw.messages) ? raw.messages : [],
    input: typeof raw.input === 'string' ? raw.input : '',
  };
}

function createTransferStateStore() {
  let state = null;
  return {
    get() {
      if (!state) return null;
      // Hand out a copy so a caller mutating the result cannot
      // corrupt the stored history.
      return { messages: [...state.messages], input: state.input };
    },
    set(raw) {
      const normalized = normalizeTransferState(raw);
      if (!normalized) return false;
      state = normalized;
      return true;
    },
    clear() {
      state = null;
    },
  };
}

module.exports = { createTransferStateStore, normalizeTransferState };
