/**
 * D4-09 — the engine-socket reconnect discipline.
 *
 * electron/wsReconnect.cjs owns what used to be a bare
 * `setTimeout(connectEngine, 3000)` inside main.cjs's close
 * handler: the handle was never stored or cleared, a superseded
 * socket's close could schedule a competing reconnect, and on
 * quit a pending timer could fire into a torn-down app. These
 * tests drive the guard with fake timers:
 *
 *   - schedule schedules exactly one reconnect per generation;
 *   - a close from a superseded generation schedules nothing;
 *   - superseding or cancelling clears the pending timer.
 */
const { createReconnectGuard } = require('../wsReconnect.cjs');

describe('wsReconnect guard (D4-09)', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('fires the reconnect once after the delay', () => {
    const reconnect = jest.fn();
    const guard = createReconnectGuard({ reconnect, delayMs: 3000 });
    const gen = guard.nextGeneration();
    guard.schedule(gen);
    expect(reconnect).not.toHaveBeenCalled();
    jest.advanceTimersByTime(3000);
    expect(reconnect).toHaveBeenCalledTimes(1);
  });

  it('a stale generation cannot schedule — a superseded socket close is ignored', () => {
    const reconnect = jest.fn();
    const guard = createReconnectGuard({ reconnect, delayMs: 3000 });
    const old = guard.nextGeneration();
    guard.nextGeneration(); // a newer socket now exists
    guard.schedule(old);
    jest.advanceTimersByTime(10000);
    expect(reconnect).not.toHaveBeenCalled();
  });

  it('scheduling twice for one close still fires only once', () => {
    const reconnect = jest.fn();
    const guard = createReconnectGuard({ reconnect, delayMs: 3000 });
    const gen = guard.nextGeneration();
    guard.schedule(gen);
    guard.schedule(gen);
    jest.advanceTimersByTime(3000);
    expect(reconnect).toHaveBeenCalledTimes(1);
  });

  it('superseding clears a pending reconnect (the new socket owns the schedule)', () => {
    const reconnect = jest.fn();
    const guard = createReconnectGuard({ reconnect, delayMs: 3000 });
    const gen = guard.nextGeneration();
    guard.schedule(gen);
    guard.nextGeneration(); // replacement socket connected meanwhile
    jest.advanceTimersByTime(10000);
    expect(reconnect).not.toHaveBeenCalled();
  });

  it('cancel clears the pending timer — nothing fires after quit', () => {
    const reconnect = jest.fn();
    const guard = createReconnectGuard({ reconnect, delayMs: 3000 });
    const gen = guard.nextGeneration();
    guard.schedule(gen);
    guard.cancel();
    jest.advanceTimersByTime(10000);
    expect(reconnect).not.toHaveBeenCalled();
  });

  it('reports whether a reconnect is pending', () => {
    const guard = createReconnectGuard({ reconnect: jest.fn(), delayMs: 3000 });
    const gen = guard.nextGeneration();
    expect(guard.isPending()).toBe(false);
    guard.schedule(gen);
    expect(guard.isPending()).toBe(true);
    jest.advanceTimersByTime(3000);
    expect(guard.isPending()).toBe(false);
  });
});
