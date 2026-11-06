'use strict';

// Engine-socket reconnect discipline (D4-09).
//
// main.cjs used to schedule reconnects with a bare
// `setTimeout(connectEngine, 3000)` inside the socket's close
// handler: the handle was never stored, never cleared, and the
// handler never checked whether its socket was still the current
// one — a superseded client's close could schedule a competing
// reconnect, and on quit a pending timer could fire into a
// half-torn-down app. The scheduling state lives here instead,
// generation-guarded, so Jest can drive it without Electron:
//
//   - every new socket takes the next generation (nextGeneration);
//     taking one also clears any pending timer — the new socket
//     owns the schedule now;
//   - schedule(generation) arms the single reconnect timer, and
//     only for the CURRENT generation — a stale socket's close is
//     ignored, and arming twice is a no-op;
//   - cancel() drops the pending timer (app quit).

const RECONNECT_DELAY_MS = 3000;

function createReconnectGuard(deps = {}) {
  const reconnect = deps.reconnect || (() => {});
  const delayMs = deps.delayMs ?? RECONNECT_DELAY_MS;
  const timers = deps.timers || { setTimeout, clearTimeout };

  let generation = 0;
  let timer = null;

  function clearPending() {
    if (timer !== null) {
      timers.clearTimeout(timer);
      timer = null;
    }
  }

  return {
    nextGeneration() {
      generation += 1;
      clearPending();
      return generation;
    },
    currentGeneration() {
      return generation;
    },
    schedule(forGeneration) {
      if (forGeneration !== generation) return;
      if (timer !== null) return;
      timer = timers.setTimeout(() => {
        timer = null;
        reconnect();
      }, delayMs);
    },
    cancel() {
      clearPending();
    },
    isPending() {
      return timer !== null;
    },
  };
}

module.exports = { createReconnectGuard, RECONNECT_DELAY_MS };
