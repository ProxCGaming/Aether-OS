/**
 * Open Point 13 (DECIDED by the owner, 1 Nov 2026) — the snip
 * hotkey is user-changeable in Settings, with default Ctrl+S on
 * Windows/Linux and Cmd+S on macOS. The PRD's recommended
 * Ctrl+Shift+S was explicitly NOT taken.
 *
 * electron/snipShortcut.cjs owns the main-process resolution:
 * which accelerator should be active given the stored preference
 * (engine user config `snip_hotkey`) and the platform. These tests
 * pin that resolution without Electron: default per platform,
 * stored preference wins, the pre-decision shipped default is
 * treated as "no preference" (it was never a deliberate user
 * choice), and an invalid stored value falls back to the default
 * with the failure flagged for surfacing.
 */
const {
  LEGACY_DEFAULT_SNIP_ACCELERATOR,
  defaultSnipAccelerator,
  resolveSnipAccelerator,
} = require('../snipShortcut.cjs');

describe('defaultSnipAccelerator (Open Point 13 decision)', () => {
  it('is Cmd+S on macOS', () => {
    expect(defaultSnipAccelerator('darwin')).toBe('Command+S');
  });

  it('is Ctrl+S on Windows and Linux', () => {
    expect(defaultSnipAccelerator('win32')).toBe('Control+S');
    expect(defaultSnipAccelerator('linux')).toBe('Control+S');
  });

  it('is never the rejected Ctrl+Shift+S recommendation', () => {
    for (const platform of ['darwin', 'win32', 'linux']) {
      expect(defaultSnipAccelerator(platform)).not.toBe('CommandOrControl+Shift+S');
      expect(defaultSnipAccelerator(platform)).not.toContain('Shift');
    }
  });
});

describe('resolveSnipAccelerator', () => {
  it('no stored preference → the platform default', () => {
    for (const stored of [undefined, null, '']) {
      const mac = resolveSnipAccelerator({ stored, platform: 'darwin' });
      expect(mac.accelerator).toBe('Command+S');
      expect(mac.source).toBe('default');
      const win = resolveSnipAccelerator({ stored, platform: 'win32' });
      expect(win.accelerator).toBe('Control+S');
      expect(win.source).toBe('default');
    }
  });

  it('a stored preference wins over the default', () => {
    const resolved = resolveSnipAccelerator({ stored: 'Ctrl+Alt+S', platform: 'win32' });
    expect(resolved.accelerator).toBe('Ctrl+Alt+S');
    expect(resolved.source).toBe('stored');
    expect(resolved.storedInvalid).toBe(false);
  });

  it('the pre-decision shipped default counts as no preference', () => {
    // Every install that never touched the setting carries the old
    // engine default 'CommandOrControl+Shift+S'; the owner's
    // decision replaces that default, so it must not pin users to
    // the rejected accelerator.
    const resolved = resolveSnipAccelerator({
      stored: LEGACY_DEFAULT_SNIP_ACCELERATOR,
      platform: 'linux',
    });
    expect(resolved.accelerator).toBe('Control+S');
    expect(resolved.source).toBe('default');
    expect(resolved.migratedLegacyDefault).toBe(true);
  });

  it('an invalid stored value falls back to the default and says so', () => {
    for (const stored of ['Banana', 'Ctrl+', 'S', 'Ctrl+Shift', 42]) {
      const resolved = resolveSnipAccelerator({ stored, platform: 'darwin' });
      expect(resolved.accelerator).toBe('Command+S');
      expect(resolved.source).toBe('default');
      expect(resolved.storedInvalid).toBe(true);
    }
  });

  it('a valid stored value is not flagged invalid', () => {
    const resolved = resolveSnipAccelerator({ stored: 'CommandOrControl+Alt+S', platform: 'linux' });
    expect(resolved.accelerator).toBe('CommandOrControl+Alt+S');
    expect(resolved.storedInvalid).toBe(false);
    expect(resolved.migratedLegacyDefault).toBe(false);
  });
});

describe('main.cjs registration contract (source pins)', () => {
  // main.cjs cannot be required in Jest (single-instance lock,
  // windows, socket on load) — the established pattern for it is
  // source-contract pins (see shortcutsQuit.test.js). Open Point
  // 13's failure rule: a failed registration must leave the
  // previous working hotkey active, so the helper must attempt
  // the NEW registration before unregistering the old accelerator,
  // and roll its pointer back when registration fails.
  const fs = require('fs');
  const path = require('path');
  const mainSource = fs.readFileSync(path.join(__dirname, '..', '..', 'main.cjs'), 'utf8');

  function snipHelperBody() {
    const start = mainSource.indexOf('function registerSnipGlobalShortcut');
    expect(start).toBeGreaterThan(-1);
    const end = mainSource.indexOf('\n}\n', start);
    return mainSource.slice(start, end);
  }

  it('the current shortcut starts at the decided platform default', () => {
    expect(mainSource).toContain('defaultSnipAccelerator(process.platform)');
    expect(mainSource).not.toContain("let currentSnipShortcut = 'CommandOrControl+Shift+S'");
  });

  it('registers the new accelerator before unregistering the old one', () => {
    const body = snipHelperBody();
    const registerIdx = body.indexOf('globalShortcut.register(');
    const unregisterIdx = body.indexOf('globalShortcut.unregister(');
    expect(registerIdx).toBeGreaterThan(-1);
    expect(unregisterIdx).toBeGreaterThan(registerIdx);
  });

  it('rolls back to the previous accelerator when registration fails', () => {
    const body = snipHelperBody();
    expect(body).toContain('currentSnipShortcut = previous');
    expect(body).toContain('return false');
  });

  it('the register-snip-shortcut IPC reports success or failure honestly', () => {
    const start = mainSource.indexOf("ipcMain.handle('register-snip-shortcut'");
    expect(start).toBeGreaterThan(-1);
    const body = mainSource.slice(start, mainSource.indexOf('});', start));
    expect(body).toContain('ok');
    expect(body).toContain('currentSnipShortcut');
    // S3-M3 sender validation stays in front of the handler.
    expect(body).toContain('isTrustedIpcSender');
    expect(body).toContain('validateAccelerator');
  });
});
