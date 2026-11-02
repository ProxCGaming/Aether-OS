/**
 * Open Point 13 (DECIDED by the owner, 1 Nov 2026) — the snip
 * hotkey is user-changeable in Settings, with default Ctrl+S on
 * Windows/Linux and Cmd+S on macOS. The PRD's recommended
 * Ctrl+Shift+S was explicitly NOT taken.
 *
 * The stored preference lives in the engine user config
 * (`snip_hotkey`, persisted through CONFIG_UPDATE_REQUEST and
 * served back in CONFIG_GET_RESPONSE). The renderer resolves it
 * and applies it in the main process through the
 * register-snip-shortcut IPC; this module owns the main-process
 * side of that resolution so it can be pinned without Electron.
 *
 * One migration rule: the pre-decision builds shipped
 * 'CommandOrControl+Shift+S' as the engine-side default, so every
 * install that never touched the setting carries exactly that
 * value. It was never a deliberate user choice, so it resolves as
 * "no preference" and the decided platform default applies.
 */
const { validateAccelerator } = require('./ipcValidation.cjs');

const LEGACY_DEFAULT_SNIP_ACCELERATOR = 'CommandOrControl+Shift+S';

function defaultSnipAccelerator(platform) {
  return platform === 'darwin' ? 'Command+S' : 'Control+S';
}

/**
 * Resolve the accelerator that should be active.
 *
 * Returns { accelerator, source, storedInvalid,
 * migratedLegacyDefault }: `source` is 'stored' when the user's
 * own value won and 'default' otherwise; `storedInvalid` flags a
 * stored value that could not be used (the caller surfaces it —
 * the fallback is never silent); `migratedLegacyDefault` flags
 * the pre-decision default being treated as unset.
 */
function resolveSnipAccelerator({ stored, platform } = {}) {
  const fallback = defaultSnipAccelerator(platform);
  const asDefault = (flags) => ({
    accelerator: fallback,
    source: 'default',
    storedInvalid: false,
    migratedLegacyDefault: false,
    ...flags,
  });
  if (stored === undefined || stored === null || stored === '') {
    return asDefault({});
  }
  if (typeof stored !== 'string') {
    return asDefault({ storedInvalid: true });
  }
  const value = stored.trim();
  if (value === '') return asDefault({});
  if (value === LEGACY_DEFAULT_SNIP_ACCELERATOR) {
    return asDefault({ migratedLegacyDefault: true });
  }
  const check = validateAccelerator(value);
  if (!check.ok) {
    return asDefault({ storedInvalid: true });
  }
  return {
    accelerator: check.value,
    source: 'stored',
    storedInvalid: false,
    migratedLegacyDefault: false,
  };
}

module.exports = {
  LEGACY_DEFAULT_SNIP_ACCELERATOR,
  defaultSnipAccelerator,
  resolveSnipAccelerator,
};
