/**
 * S3-M7 — the floating-window transfer state (the FULL chat history +
 * the in-progress draft input) must never be persisted in plaintext
 * web storage. It used to sit in localStorage under
 * 'floating_transfer_state', readable by any script running in the
 * renderer. It now lives only in Electron main-process memory:
 *
 *   - electron/transferState.cjs holds it (this file unit-tests the
 *     store in isolation, like windowState.cjs / notify.cjs);
 *   - main.cjs exposes set/get/clear IPC handlers and clears the
 *     store when the floating window closes for real, handing the
 *     final snapshot to the main window WITH the 'chat-attached'
 *     event so re-attach still restores the floating edits;
 *   - preload.cjs exposes the three accessors on electronAPI and
 *     passes the attach snapshot through to the callback.
 *
 * main.cjs cannot be required in Jest (single-instance lock, windows,
 * engine socket on load), so — like preload-no-token.test.js and
 * shortcutsQuit.test.js — the main/preload halves pin the source
 * contract.
 */
const fs = require('fs');
const path = require('path');

const { createTransferStateStore, normalizeTransferState } = require('../transferState.cjs');

const appRoot = path.join(__dirname, '..', '..');
const mainSrc = fs.readFileSync(path.join(appRoot, 'main.cjs'), 'utf8');
const preloadSrc = fs.readFileSync(path.join(appRoot, 'preload.cjs'), 'utf8');

describe('transferState store (main-process memory)', () => {
  it('starts empty', () => {
    expect(createTransferStateStore().get()).toBeNull();
  });

  it('round-trips messages and input', () => {
    const store = createTransferStateStore();
    const state = {
      messages: [{ role: 'user', content: 'hello' }, { role: 'agent', content: 'hi', isFinal: true }],
      input: 'draft text',
    };
    expect(store.set(state)).toBe(true);
    expect(store.get()).toEqual(state);
  });

  it('set replaces the previous state wholesale', () => {
    const store = createTransferStateStore();
    store.set({ messages: [{ role: 'user', content: 'old' }], input: 'old draft' });
    store.set({ messages: [{ role: 'user', content: 'new' }], input: '' });
    expect(store.get()).toEqual({ messages: [{ role: 'user', content: 'new' }], input: '' });
  });

  it('clear drops the state (floating window closed for real)', () => {
    const store = createTransferStateStore();
    store.set({ messages: [{ role: 'user', content: 'secret history' }], input: 'x' });
    store.clear();
    expect(store.get()).toBeNull();
  });

  it('get returns a copy — mutating it cannot corrupt the stored history', () => {
    const store = createTransferStateStore();
    store.set({ messages: [{ role: 'user', content: 'a' }], input: 'd' });
    const out = store.get();
    out.messages.push({ role: 'user', content: 'injected' });
    out.input = 'tampered';
    expect(store.get()).toEqual({ messages: [{ role: 'user', content: 'a' }], input: 'd' });
  });

  it('rejects non-object state and keeps the previous value', () => {
    const store = createTransferStateStore();
    store.set({ messages: [], input: 'keep' });
    expect(store.set(null)).toBe(false);
    expect(store.set('a string')).toBe(false);
    expect(store.set([{ messages: [] }])).toBe(false);
    expect(store.get()).toEqual({ messages: [], input: 'keep' });
  });

  it('normalizes a partial state instead of trusting renderer shapes', () => {
    expect(normalizeTransferState({})).toEqual({ messages: [], input: '' });
    expect(normalizeTransferState({ messages: 'nope', input: 42 })).toEqual({ messages: [], input: '' });
    expect(normalizeTransferState(null)).toBeNull();
  });
});

describe('S3-M7 main-process IPC contract', () => {
  it('main.cjs holds the transfer state in a main-process store', () => {
    expect(mainSrc).toContain("require('./electron/transferState.cjs')");
    expect(mainSrc).toContain('createTransferStateStore()');
  });

  it('registers set/get/clear IPC handlers backed by the store', () => {
    expect(mainSrc).toContain("ipcMain.handle('set-floating-transfer-state'");
    expect(mainSrc).toContain("ipcMain.handle('get-floating-transfer-state'");
    expect(mainSrc).toContain("ipcMain.handle('clear-floating-transfer-state'");
    expect(mainSrc).toContain('floatingTransferState.set(');
    expect(mainSrc).toContain('floatingTransferState.get()');
    expect(mainSrc).toContain('floatingTransferState.clear()');
  });

  it('clears the store when the floating window closes, handing the snapshot to the main window with chat-attached', () => {
    const start = mainSrc.indexOf("floatingChatWindow.on('closed'");
    expect(start).toBeGreaterThan(-1);
    const body = mainSrc.slice(start, mainSrc.indexOf('});', start));
    // Snapshot first, then clear, then notify — in that order, so the
    // main window still receives the floating edits even though the
    // store itself is wiped at close.
    const snapshotAt = body.indexOf('floatingTransferState.get()');
    const clearAt = body.indexOf('floatingTransferState.clear()');
    const sendAt = body.indexOf("webContents.send('chat-attached'");
    expect(snapshotAt).toBeGreaterThan(-1);
    expect(clearAt).toBeGreaterThan(snapshotAt);
    expect(sendAt).toBeGreaterThan(clearAt);
    expect(body).toContain('transferSnapshot');
  });
});

describe('S3-M7 preload contract', () => {
  it('exposes set/get/clear transfer accessors on electronAPI', () => {
    expect(preloadSrc).toContain('setFloatingTransferState');
    expect(preloadSrc).toContain("ipcRenderer.invoke('set-floating-transfer-state'");
    expect(preloadSrc).toContain('getFloatingTransferState');
    expect(preloadSrc).toContain("ipcRenderer.invoke('get-floating-transfer-state'");
    expect(preloadSrc).toContain('clearFloatingTransferState');
    expect(preloadSrc).toContain("ipcRenderer.invoke('clear-floating-transfer-state'");
  });

  it('onChatAttached passes the transfer snapshot through to the callback', () => {
    // The snapshot rides the 'chat-attached' event because the store
    // is already cleared by the time the main window reacts.
    expect(preloadSrc).toMatch(/ipcRenderer\.on\('chat-attached', handler\)/);
    expect(preloadSrc).toMatch(/onChatAttached:[\s\S]*?callback\(state\)/);
  });
});
