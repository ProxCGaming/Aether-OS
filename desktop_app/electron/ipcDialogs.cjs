'use strict';

// Native-dialog IPC actions (Wave 5 split, Annex C row 12).
//
// The two folder-picker channels (select-directory,
// show-open-dialog) used to run their dialog calls inline in
// main.cjs. The calls live here, behind createIpcDialogs with
// injected BrowserWindow/dialog, so Jest can drive them without
// Electron. The registrations stay in main.cjs, behind the
// S3-M3 sender guard — and show-open-dialog's options are
// validated there too (ipcValidation.cjs); this module receives
// only the validated options and owns the dialog itself.

function createIpcDialogs(deps = {}) {
  const BrowserWindow = deps.BrowserWindow;
  const dialog = deps.dialog;

  async function selectDirectory(event) {
    const win = BrowserWindow.fromWebContents(event.sender);
    const result = await dialog.showOpenDialog(win, {
      properties: ['openDirectory', 'createDirectory'],
      title: 'Select Download Directory'
    });
    if (result.canceled) {
      return null;
    } else {
      return result.filePaths[0];
    }
  }

  async function showOpenDialog(event, validatedOptions) {
    const win = BrowserWindow.fromWebContents(event.sender);
    const result = await dialog.showOpenDialog(win, validatedOptions || {
      properties: ['openDirectory'],
      title: 'Select Folder'
    });
    if (result.canceled) return null;
    return result.filePaths[0];
  }

  return { selectDirectory, showOpenDialog };
}

module.exports = { createIpcDialogs };
