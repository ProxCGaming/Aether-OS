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

  test('S1-M3: connection is built with a token-free URL and the header carrier', () => {
    // The URL handed to the ws client is a bare constant — no query
    // string is ever constructed — and the token rides only in the
    // Authorization header option of the same construction. The
    // Electron main process (Node `ws`) can set headers, which the
    // browser WebSocket API cannot; the renderer never connects
    // directly, so the header route is the one this layer can send.
    expect(mainSrc).toContain("const wsUrl = 'ws://127.0.0.1:8000/ws/tasks';");
    expect(mainSrc).not.toContain('wsUrl +');
    expect(mainSrc).not.toContain('wsUrl?');
    expect(mainSrc).toContain(
      'new WebSocket(wsUrl, { headers: { Authorization: `Bearer ${token}` } })');
  });

  test('S1-M3: no engine client code in the desktop app builds a ?token= URL', () => {
    const socketCtx = fs.readFileSync(
      path.join(__dirname, '..', 'contexts', 'SocketContext.jsx'), 'utf8');
    expect(socketCtx).not.toContain('?token=');
    expect(socketCtx).not.toContain('new WebSocket');
  });

  test('renderer still reaches the engine via the main-owned socket', () => {
    expect(preloadSrc).toContain('sendEngineMessage');
    expect(preloadSrc).toContain('onEngineMessage');
    expect(mainSrc).toContain("ipcMain.on('engine-send'");
    expect(mainSrc).toContain("webContents.send('engine-message'");
  });
});
