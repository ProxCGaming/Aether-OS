/**
 * Open Point 13 — renderer mirror of the snip-hotkey resolution
 * (src/snipHotkey.js). The main process (electron/snipShortcut.cjs)
 * is authoritative; this side resolves what Settings displays and
 * what App applies at startup, with the same rules: platform
 * default (Ctrl+S / Cmd+S), stored preference wins, the
 * pre-decision shipped default counts as unset, invalid stored
 * values fall back to the default flagged as invalid.
 */
import {
  LEGACY_DEFAULT_SNIP_HOTKEY,
  defaultSnipHotkey,
  resolveSnipHotkey,
  isValidAccelerator,
} from '../snipHotkey';

describe('renderer snip hotkey resolution (Open Point 13)', () => {
  it('defaults to Cmd+S on macOS and Ctrl+S elsewhere', () => {
    expect(defaultSnipHotkey('darwin')).toBe('Command+S');
    expect(defaultSnipHotkey('win32')).toBe('Control+S');
    expect(defaultSnipHotkey('linux')).toBe('Control+S');
  });

  it('no stored value resolves to the platform default', () => {
    expect(resolveSnipHotkey(undefined, 'darwin').accelerator).toBe('Command+S');
    expect(resolveSnipHotkey('', 'win32').accelerator).toBe('Control+S');
    expect(resolveSnipHotkey(null, 'linux').source).toBe('default');
  });

  it('a stored preference wins', () => {
    const resolved = resolveSnipHotkey('Ctrl+Alt+S', 'win32');
    expect(resolved.accelerator).toBe('Ctrl+Alt+S');
    expect(resolved.source).toBe('stored');
  });

  it('the legacy shipped default resolves to the decided default', () => {
    const resolved = resolveSnipHotkey(LEGACY_DEFAULT_SNIP_HOTKEY, 'linux');
    expect(resolved.accelerator).toBe('Control+S');
    expect(resolved.migratedLegacyDefault).toBe(true);
  });

  it('an invalid stored value falls back to the default, flagged', () => {
    const resolved = resolveSnipHotkey('Banana', 'darwin');
    expect(resolved.accelerator).toBe('Command+S');
    expect(resolved.storedInvalid).toBe(true);
  });

  it('isValidAccelerator mirrors the main-process rules', () => {
    expect(isValidAccelerator('Control+S')).toBe(true);
    expect(isValidAccelerator('Command+S')).toBe(true);
    expect(isValidAccelerator('CommandOrControl+Alt+S')).toBe(true);
    expect(isValidAccelerator('F5')).toBe(true);
    expect(isValidAccelerator('Banana')).toBe(false);
    expect(isValidAccelerator('S')).toBe(false);
    expect(isValidAccelerator('Ctrl+')).toBe(false);
    expect(isValidAccelerator('')).toBe(false);
    expect(isValidAccelerator(42)).toBe(false);
  });
});
