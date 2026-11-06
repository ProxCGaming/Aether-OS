/**
 * Epic 6, E6-01 — shell helpers: window-state store + notify gate.
 */
const { createWindowStateStore, validateBounds, ensureVisibleOnDisplays, DEFAULT_BOUNDS } = require('../windowState.cjs');
const { shouldNotify, notificationContent } = require('../notify.cjs');

function fakeFs(initial = {}) {
  const files = { ...initial };
  const ops = [];
  return {
    files,
    ops,
    readFileSync(p) {
      if (!(p in files)) throw new Error('ENOENT');
      return files[p];
    },
    writeFileSync(p, data) {
      ops.push(`write ${p}`);
      files[p] = data;
    },
    renameSync(from, to) {
      ops.push(`rename ${from} -> ${to}`);
      if (!(from in files)) throw new Error('ENOENT');
      files[to] = files[from];
      delete files[from];
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

  it('saves atomically: temp file + rename, never a direct overwrite (D4-08)', () => {
    // The repo's own standard for state files (token writer, engine
    // config) is temp-file + rename. A crash mid-write must leave
    // the previous window-state.json intact, not truncated.
    const fs = fakeFs();
    const store = createWindowStateStore({ filePath: '/x/state.json', fs });
    expect(store.save({ width: 1440, height: 900, x: 10, y: 20 })).toBe(true);
    expect(fs.ops.some((op) => op === 'write /x/state.json')).toBe(false);
    expect(fs.ops.some((op) => op.startsWith('rename ') && op.endsWith('-> /x/state.json'))).toBe(true);
    expect(JSON.parse(fs.files['/x/state.json'])).toEqual({ width: 1440, height: 900, x: 10, y: 20 });
    // No temp file is left behind.
    expect(Object.keys(fs.files)).toEqual(['/x/state.json']);
  });
});

describe('windowState visibility clamp (D4-02)', () => {
  const primary = { workArea: { x: 0, y: 0, width: 1920, height: 1040 } };
  const second = { workArea: { x: 1920, y: 0, width: 1280, height: 1024 } };
  const left = { workArea: { x: -1280, y: 0, width: 1280, height: 1024 } };

  it('keeps a position that meaningfully intersects a connected display', () => {
    const bounds = { width: 1280, height: 800, x: 100, y: 50 };
    expect(ensureVisibleOnDisplays(bounds, [primary, second])).toEqual(bounds);
  });

  it('drops the position when its display was unplugged (window would open invisibly)', () => {
    const bounds = { width: 1280, height: 800, x: 2000, y: 60 };
    const out = ensureVisibleOnDisplays(bounds, [primary]);
    expect(out).toEqual({ width: 1280, height: 800 });
  });

  it('keeps a position on a negative-coordinate display that is still connected', () => {
    const bounds = { width: 1000, height: 700, x: -1200, y: 40 };
    expect(ensureVisibleOnDisplays(bounds, [primary, left])).toEqual(bounds);
  });

  it('drops a position only touching a display by a useless sliver', () => {
    // 20px of a 1280-wide window on screen is not a visible window.
    const bounds = { width: 1280, height: 800, x: 1900, y: 0 };
    const out = ensureVisibleOnDisplays(bounds, [primary]);
    expect(out.x).toBeUndefined();
    expect(out.y).toBeUndefined();
  });

  it('leaves bounds untouched when there is no position or no display info', () => {
    expect(ensureVisibleOnDisplays({ width: 1280, height: 800 }, [primary]))
      .toEqual({ width: 1280, height: 800 });
    const positioned = { width: 1280, height: 800, x: 5000, y: 0 };
    expect(ensureVisibleOnDisplays(positioned, [])).toEqual(positioned);
    expect(ensureVisibleOnDisplays(positioned, null)).toEqual(positioned);
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
