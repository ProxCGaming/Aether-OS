/**
 * Wave 5 (Annex C row 12): the engine-socket relay, extracted
 * from main.cjs into electron/engineSocket.cjs. main.cjs still
 * owns the token read, the authenticated socket construction
 * (Phase 10.2 B1 — pinned there by preload-no-token.test.js) and
 * the D4-09 reconnect guard wiring (pinned there by
 * engineLifecycle.test.js); this suite wires the module the way
 * main does — a real createReconnectGuard driving the schedule —
 * and drives the lifecycle the module owns: token gating, status
 * broadcasts, superseded-socket silence, and send/open gating.
 */
const { EventEmitter } = require('events');
const { createEngineSocket } = require('../engineSocket.cjs');
const { createReconnectGuard } = require('../wsReconnect.cjs');

class FakeSocket extends EventEmitter {
  constructor() {
    super();
    this.readyState = 0; // CONNECTING
    this.sent = [];
    this.closeCalls = 0;
  }

  send(text) {
    this.sent.push(text);
  }

  close() {
    this.closeCalls += 1;
    this.readyState = 2; // CLOSING, until the test emits 'close'
  }

  open() {
    this.readyState = 1; // OPEN
    this.emit('open');
  }

  closeFromServer(code = 1006, reason = '') {
    this.readyState = 3; // CLOSED
    this.emit('close', code, Buffer.from(reason));
  }
}

function makeRelay({ token = 'tok-1' } = {}) {
  const state = {
    token,
    sockets: [],
    broadcasts: [],
    messages: [],
    logs: [],
    warns: [],
    errors: [],
  };
  // The guard wiring mirrors main.cjs: the guard and its
  // generation counter live outside the module; the module
  // drives them through the two deps.
  let relay;
  const guard = createReconnectGuard({ reconnect: () => relay.connect() });
  let generation = 0;
  relay = createEngineSocket({
    createSocket: (tok) => {
      const socket = new FakeSocket();
      socket.token = tok;
      state.sockets.push(socket);
      return socket;
    },
    getToken: () => state.token,
    broadcast: (msg) => state.broadcasts.push(msg),
    onMessage: (msgStr) => state.messages.push(msgStr),
    log: (...args) => state.logs.push(args.join(' ')),
    error: (...args) => state.errors.push(args.map(String).join(' ')),
    openState: 1,
    nextGeneration: () => {
      generation = guard.nextGeneration();
    },
    scheduleReconnect: () => guard.schedule(generation),
    onTokenMissing: () => state.warns.push('No engine token is available yet; will retry.'),
  });
  return { relay, state };
}

describe('createEngineSocket', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('connects with the token and broadcasts connected on open', () => {
    const { relay, state } = makeRelay();
    relay.connect();
    expect(state.sockets).toHaveLength(1);
    expect(state.sockets[0].token).toBe('tok-1');
    state.sockets[0].open();
    expect(state.broadcasts).toEqual([{ type: '_ws_status', status: 'connected' }]);
    expect(state.logs.join('\n')).toContain('Main process connected to Engine WebSocket.');
    expect(relay.isOpen()).toBe(true);
  });

  it('without a token it opens no socket and retries on the guard schedule (D4-09)', () => {
    const { relay, state } = makeRelay({ token: null });
    relay.connect();
    expect(state.sockets).toHaveLength(0);
    expect(state.warns).toEqual(['No engine token is available yet; will retry.']);
    // The token lands when the engine finishes starting; the
    // scheduled retry picks it up.
    state.token = 'tok-late';
    jest.advanceTimersByTime(3000);
    expect(state.sockets).toHaveLength(1);
    expect(state.sockets[0].token).toBe('tok-late');
  });

  it('forwards engine messages to broadcast and onMessage as strings', () => {
    const { relay, state } = makeRelay();
    relay.connect();
    state.sockets[0].open();
    state.sockets[0].emit('message', Buffer.from('{"type":"HELLO"}'));
    expect(state.broadcasts).toContain('{"type":"HELLO"}');
    expect(state.messages).toEqual(['{"type":"HELLO"}']);
  });

  it('a current socket close broadcasts disconnected and schedules one reconnect', () => {
    const { relay, state } = makeRelay();
    relay.connect();
    state.sockets[0].open();
    state.sockets[0].closeFromServer(1006, 'gone');
    expect(state.broadcasts).toContainEqual({ type: '_ws_status', status: 'disconnected' });
    expect(relay.isOpen()).toBe(false);
    jest.advanceTimersByTime(3000);
    expect(state.sockets).toHaveLength(2);
  });

  it('a superseded socket close is silent: no broadcast, no reconnect of its own (D4-09)', () => {
    const { relay, state } = makeRelay();
    relay.connect();
    const first = state.sockets[0];
    first.open();
    relay.connect(); // supersedes: closes the first socket
    expect(first.closeCalls).toBe(1);
    const second = state.sockets[1];
    second.open();
    state.broadcasts.length = 0;
    first.closeFromServer(1006, 'stale');
    expect(state.broadcasts).toEqual([]);
    jest.advanceTimersByTime(3000);
    expect(state.sockets).toHaveLength(2); // no third socket
    expect(relay.isOpen()).toBe(true);
  });

  it('send forwards only while the socket is open', () => {
    const { relay, state } = makeRelay();
    expect(relay.isOpen()).toBeNull();
    relay.send('early');
    relay.connect();
    relay.send('connecting');
    expect(state.sockets[0].sent).toEqual([]);
    state.sockets[0].open();
    relay.send('{"type":"PING"}');
    expect(state.sockets[0].sent).toEqual(['{"type":"PING"}']);
    state.sockets[0].closeFromServer();
    relay.send('late');
    expect(state.sockets[0].sent).toEqual(['{"type":"PING"}']);
  });

  it('socket errors are logged, never thrown', () => {
    const { relay, state } = makeRelay();
    relay.connect();
    state.sockets[0].emit('error', new Error('boom'));
    expect(state.errors.join('\n')).toContain('WebSocket error:');
    expect(state.errors.join('\n')).toContain('boom');
  });
});
