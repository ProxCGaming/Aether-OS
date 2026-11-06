'use strict';

// Engine-socket relay (Wave 5 split, Annex C row 12).
//
// The relay between the renderer and the engine used to live
// inline in main.cjs: token fetch, socket construction, the
// reconnect schedule and the _ws_status broadcasts. The
// connection lifecycle lives here instead, behind
// createEngineSocket, so Jest can drive it without Electron.
// What stays in main.cjs and arrives as deps:
//
//   - the socket construction (createSocket): main keeps the
//     bare-constant URL and the construction that sends the
//     token ONLY as an Authorization header — Phase 10.2 B1,
//     the renderer never holds the token — and that literal
//     construction is pinned to main.cjs by
//     src/__tests__/preload-no-token.test.js;
//   - the token read (getToken): the spawned engine's token
//     file belongs to the engine-lifecycle wiring main owns;
//   - the D4-09 reconnect guard and its generation counter
//     (nextGeneration / scheduleReconnect): the guard is wired
//     in main, next to the will-quit cancel that shares it, and
//     its wiring surface is pinned there by
//     electron/__tests__/engineLifecycle.test.js — this module
//     drives it, it does not own it;
//   - the tokenless-engine warning (onTokenMissing): the
//     message is main's log line; the DECISION — never open a
//     socket we know will be refused — is made here;
//   - the renderer fan-out (broadcast): window wiring. This
//     module decides WHEN a message is broadcast (socket open /
//     current-socket close), never where it lands.

function createEngineSocket(deps = {}) {
  const createSocket = deps.createSocket;
  const getToken = deps.getToken || (() => null);
  const broadcast = deps.broadcast || (() => {});
  const onMessage = deps.onMessage || (() => {});
  const log = deps.log || (() => {});
  const error = deps.error || (() => {});
  const openState = deps.openState ?? 1; // WebSocket.OPEN
  const nextGeneration = deps.nextGeneration || (() => {});
  const scheduleReconnect = deps.scheduleReconnect || (() => {});
  const onTokenMissing = deps.onTokenMissing || (() => {});

  let wsClient = null;

  function connect() {
    const token = getToken();

    // Beginning a (re)connect supersedes every earlier socket
    // and clears any pending reconnect timer (the guard's
    // nextGeneration, driven from main's wiring).
    nextGeneration();

    if (wsClient) {
      wsClient.close();
    }

    if (!token) {
      // D4-09: the engine REQUIRES registration of its token —
      // a legacy engine without one refuses tokenless sockets
      // with 1008, so opening one only bought a guaranteed
      // reject + close + retry cycle. Do not open a socket we
      // know will be refused; wait for the token (it lands when
      // the engine finishes starting) and retry on the guard's
      // schedule.
      wsClient = null;
      onTokenMissing();
      scheduleReconnect();
      return;
    }

    const socket = createSocket(token);
    wsClient = socket;

    socket.on('open', () => {
      log('Main process connected to Engine WebSocket.');
      broadcast({ type: '_ws_status', status: 'connected' });
    });

    socket.on('message', (data) => {
      const msgStr = data.toString();
      broadcast(msgStr);
      onMessage(msgStr);
    });

    socket.on('close', (code, reason) => {
      const reasonText = reason ? reason.toString() : '';
      log(`Engine WebSocket closed (code=${code}, reason=${reasonText || 'none'}).`);
      // D4-09: a superseded socket's close is stale news — it
      // must not tell the renderer the (new) connection dropped,
      // and it must not schedule a reconnect of its own.
      if (wsClient !== socket) return;
      broadcast({ type: '_ws_status', status: 'disconnected' });
      scheduleReconnect();
    });

    socket.on('error', (err) => {
      error('WebSocket error:', err);
    });
  }

  return {
    connect,
    // get-ws-status: the current socket's live readyState,
    // exactly as main.cjs used to compute it (null until a
    // socket exists).
    isOpen() {
      return wsClient && wsClient.readyState === openState;
    },
    // engine-send: forward only on a live socket; a send into a
    // connecting/closing socket is dropped, as before.
    send(text) {
      if (wsClient && wsClient.readyState === openState) {
        wsClient.send(text);
      }
    },
  };
}

module.exports = { createEngineSocket };
