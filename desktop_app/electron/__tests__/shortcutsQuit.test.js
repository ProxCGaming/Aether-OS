/**
 * F-14 — pinning tests: global shortcuts must not survive app quit.
 *
 * EGM supplementary finding claimed the summon (Alt+Space) and snip
 * (CommandOrControl+Shift+S) global shortcuts survive app quit.
 * Provenance check (git log -S 'unregisterAll' / git blame) shows
 * the quit cleanup landed in 7e1ae19 ("shell standard", 31 Oct
 * 2026) — BEFORE the EGM snapshot 71594ce, whose own main.cjs
 * already contains it at lines 301-302. The finding is therefore
 * closed by evidence (F-02 pattern); these tests pin the existing
 * behaviour so it cannot silently regress.
 *
 * main.cjs cannot be required in Jest (it takes the single-
 * instance lock, opens windows and connects a socket on load),
 * so — like preload-no-token.test.js — this pins the main-process
 * source contract: the will-quit cleanup exists, every quit entry
 * point funnels through app.quit() into it, and no code path can
 * bypass it or register a shortcut after it runs.
 */
const fs = require('fs');
const path = require('path');

const mainSource = fs.readFileSync(path.join(__dirname, '..', '..', 'main.cjs'), 'utf8');

function handlerBody(eventName) {
  const start = mainSource.indexOf(`app.on('${eventName}'`);
  if (start === -1) return null;
  const end = mainSource.indexOf('});', start);
  return mainSource.slice(start, end);
}

describe('F-14 pinning: quit cleanup exists', () => {
  it("the 'will-quit' handler unregisters all global shortcuts", () => {
    const body = handlerBody('will-quit');
    expect(body).not.toBeNull();
    expect(body).toContain('globalShortcut.unregisterAll()');
  });

  it('there is exactly one unregisterAll call site in main.cjs', () => {
    expect(mainSource.match(/globalShortcut\.unregisterAll\(\)/g)).toHaveLength(1);
  });

  it('both global shortcuts are registered in the main process', () => {
    // Summon hotkey, registered once when the app is ready.
    expect(mainSource).toContain("globalShortcut.register('Alt+Space', toggleMainWindow)");
    // Snip hotkey, (re)registered through one helper that first
    // unregisters the previous accelerator.
    expect(mainSource).toContain('globalShortcut.register(currentSnipShortcut, triggerSnipFlow)');
    expect(mainSource).toContain("let currentSnipShortcut = 'CommandOrControl+Shift+S'");
    expect(mainSource).toContain('globalShortcut.unregister(currentSnipShortcut)');
    // Exactly two register call sites — no stray registrations.
    expect(mainSource.match(/globalShortcut\.register\(/g)).toHaveLength(2);
  });
});

describe('F-14 pinning: every quit path funnels through will-quit', () => {
  it('tray Quit goes through app.quit()', () => {
    expect(mainSource).toContain("{ label: 'Quit', click: () => app.quit() }");
  });

  it('window-all-closed quits on non-macOS platforms', () => {
    const body = handlerBody('window-all-closed');
    expect(body).not.toBeNull();
    expect(body).toContain("process.platform !== 'darwin'");
    expect(body).toContain('app.quit()');
  });

  it('app.quit() is the only quit mechanism — no hard-exit bypass', () => {
    // app.exit() / process.exit() would skip 'will-quit' entirely.
    expect(mainSource).not.toContain('app.exit(');
    expect(mainSource).not.toContain('process.exit(');
    // The only app.quit() callers: the single-instance guard
    // (runs before any shortcut is registered), the tray item,
    // and window-all-closed.
    expect(mainSource.match(/app\.quit\(\)/g)).toHaveLength(3);
  });

  it('nothing can cancel the quit before will-quit fires', () => {
    // A 'before-quit' handler calling preventDefault() could stop
    // the quit sequence short of 'will-quit'; there is none.
    expect(mainSource).not.toContain("'before-quit'");
    expect(mainSource).not.toContain('preventDefault');
  });
});
