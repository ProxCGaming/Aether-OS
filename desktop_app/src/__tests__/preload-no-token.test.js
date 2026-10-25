// Phase 10.2 B1: the renderer must never hold the engine token.
// The `get-auth-token` IPC handler and its `getAuthToken` preload exposure
// were deleted; the Electron main process owns the engine WebSocket and
// authenticates with an `Authorization: Bearer` header only, forwarding
// messages to the renderer over `engine-message`. This is a static
// source-level test: it reads main.cjs / preload.cjs and asserts the
// token never appears on the renderer-facing surface. (A matching
// Python guard lives in tests/test_phase10_2_token_header.py.)
const fs = require('fs');
const path = require('path');

const preloadSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'preload.cjs'), 'utf8');
const mainSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'main.cjs'), 'utf8');

describe('Phase 10.2 B1: renderer must never hold the engine token', () => {
  test('preload exposes no token accessor', () => {
    expect(preloadSrc).not.toContain('getAuthToken');
    expect(preloadSrc).not.toContain('get-auth-token');
  });

  test('main process has no get-auth-token IPC handler', () => {
    expect(mainSrc).not.toContain('get-auth-token');
    expect(mainSrc).not.toContain('getAuthToken');
  });

  test('engine WebSocket URL never carries the token in a query string', () => {
    expect(mainSrc).not.toContain('?token=');
    expect(mainSrc).not.toContain('token=${');
  });

  test('engine WebSocket sends the token in the Authorization header', () => {
    expect(mainSrc).toContain('Authorization');
    expect(mainSrc).toContain('Bearer ');
  });

  test('renderer still reaches the engine via the main-owned socket', () => {
    expect(preloadSrc).toContain('sendEngineMessage');
    expect(preloadSrc).toContain('onEngineMessage');
    expect(mainSrc).toContain("ipcMain.on('engine-send'");
    expect(mainSrc).toContain("webContents.send('engine-message'");
  });
});
