'use strict';

// Native splash preload (23 Nov 2026): the splash page's ONLY
// bridge. It exposes exactly one subscription — the stage
// channel main mirrors from electron/splashWindow.cjs's tracker
// — and nothing else: no invokes, no engine surface. Like
// preload.cjs it requires only 'electron', which is the API set
// a sandboxed preload is allowed (S3-M2).
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('splashAPI', {
  onStage: (callback) => {
    const handler = (_event, value) => callback(value);
    ipcRenderer.on('splash-stage', handler);
    return () => ipcRenderer.removeListener('splash-stage', handler);
  },
});
