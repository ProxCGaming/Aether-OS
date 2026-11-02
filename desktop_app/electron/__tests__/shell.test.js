/**
 * Epic 6, E6-01 — shell helpers: window-state store + notify gate.
 */
const { createWindowStateStore, validateBounds, DEFAULT_BOUNDS } = require('../windowState.cjs');
const { shouldNotify, notificationContent } = require('../notify.cjs');

function fakeFs(initial = {}) {
  const files = { ...initial };
  return {
    files,
    readFileSync(p) {
      if (!(p in files)) throw new Error('ENOENT');
      return files[p];
    },
    writeFileSync(p, data) {
      files[p] = data;
    },
  };
}

describe('windowState store', () => {
  it('returns defaults when no file exists', () => {
    const store = createWindowStateStore({ filePath: '/x/state.json', fs: fakeFs() });
    expect(store.load()).toEqual(DEFAULT_BOUNDS);
  });

  it('round-trips valid bounds including position', () => {
    const fs = fakeFs();
    const store = createWindowStateStore({ filePath: '/x/state.json', fs });
    expect(store.save({ width: 1440, height: 900, x: 10, y: 20 })).toBe(true);
    expect(store.load()).toEqual({ width: 1440, height: 900, x: 10, y: 20 });
  });

  it('rejects corrupt or absurd bounds', () => {
    expect(validateBounds(null)).toBeNull();
    expect(validateBounds({ width: 'wide', height: 900 })).toBeNull();
    expect(validateBounds({ width: 100, height: 900 })).toBeNull(); // below minimum
    expect(validateBounds({ width: 99999, height: 900 })).toBeNull(); // absurd
    const store = createWindowStateStore({
      filePath: '/x/state.json',
      fs: fakeFs({ '/x/state.json': '{not json' }),
    });
    expect(store.load()).toEqual(DEFAULT_BOUNDS);
  });

  it('drops a bogus position but keeps a valid size', () => {
    expect(validateBounds({ width: 800, height: 600, x: 'left', y: null })).toEqual({ width: 800, height: 600 });
  });
});

describe('notify gate', () => {
  const completed = JSON.stringify({ type: 'TASK_COMPLETED', payload: {} });
  const approval = JSON.stringify({ type: 'TOOL_APPROVAL_REQUEST', payload: { tool_name: 'run_shell' } });

  it('notifies for completion and approval when the window is not focused', () => {
    expect(shouldNotify(completed, { windowVisible: true, windowFocused: false })).toBe(true);
    expect(shouldNotify(approval, { windowVisible: false, windowFocused: false })).toBe(true);
  });

  it('stays silent when the user is looking at the app', () => {
    expect(shouldNotify(completed, { windowVisible: true, windowFocused: true })).toBe(false);
    expect(shouldNotify(approval, { windowVisible: true, windowFocused: true })).toBe(false);
  });

  it('ignores other event types and malformed messages', () => {
    expect(shouldNotify(JSON.stringify({ type: 'TASK_STREAM' }), { windowVisible: false, windowFocused: false })).toBe(false);
    expect(shouldNotify('not json', { windowVisible: false, windowFocused: false })).toBe(false);
    expect(shouldNotify(JSON.stringify({ type: '_ws_status' }), { windowVisible: false, windowFocused: false })).toBe(false);
  });

  it('builds honest notification content naming the tool for approvals', () => {
    expect(notificationContent(completed).title).toContain('task complete');
    expect(notificationContent(approval).body).toContain('run_shell');
    expect(notificationContent('junk')).toBeNull();
  });
});
