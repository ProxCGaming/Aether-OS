/**
 * Wave 1 slice 4: the startup gate's IPC wiring. main.cjs cannot be
 * imported in Jest (it boots Electron), so — the preload-no-token
 * pattern — these assert the two sides of each channel agree:
 * preload exposes the surface and invokes/sends the channel names
 * main handles/broadcasts.
 */
const fs = require('fs');
const path = require('path');

const appRoot = path.join(__dirname, '..', '..');
const mainSrc = fs.readFileSync(path.join(appRoot, 'main.cjs'), 'utf8');
const preloadSrc = fs.readFileSync(path.join(appRoot, 'preload.cjs'), 'utf8');

describe('startup gate IPC wiring', () => {
  it('preload exposes the engine-state surface', () => {
    expect(preloadSrc).toContain('getEngineState');
    expect(preloadSrc).toContain("ipcRenderer.invoke('get-engine-state')");
    expect(preloadSrc).toContain('onEngineState');
    expect(preloadSrc).toContain("ipcRenderer.on('engine-state'");
    expect(preloadSrc).toContain('retryEngineStart');
    expect(preloadSrc).toContain("ipcRenderer.invoke('retry-engine-start')");
    expect(preloadSrc).toContain('openEngineLog');
    expect(preloadSrc).toContain("ipcRenderer.invoke('open-engine-log')");
  });

  it('main handles the gate channels and broadcasts engine-state', () => {
    expect(mainSrc).toContain("ipcMain.handle('get-engine-state'");
    expect(mainSrc).toContain("ipcMain.handle('retry-engine-start'");
    expect(mainSrc).toContain("ipcMain.handle('open-engine-log'");
    expect(mainSrc).toContain("webContents.send('engine-state'");
  });

  it('main derives failed strictly from the lifecycle settling at mode none', () => {
    expect(mainSrc).toContain("result.mode === 'none' ? 'failed' : 'started'");
    // Retry is gated on the definitive-failure mode, not on time.
    expect(mainSrc).toContain("getMode() === 'none'");
    // The startup outcome comes from the tracked start wrapper.
    expect(mainSrc).toContain('startEngineAndTrack');
  });

  it('opening the log resolves the path from the lifecycle, never the renderer', () => {
    const handlerStart = mainSrc.indexOf("ipcMain.handle('open-engine-log'");
    const handler = mainSrc.slice(handlerStart, handlerStart + 900);
    expect(handler).toContain('engineLifecycle.logFilePath()');
    expect(handler).toContain('shell.openPath(logPath)');
  });
});
