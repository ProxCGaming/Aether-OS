'use strict';

// Native splash (23 Nov 2026): the stage tracker + reveal rule.
// The splash may only give way to the main window once the
// interface has actually loaded AND startup has settled (the
// renderer's gate latch, an engine failure, or the fallback
// budget) — revealing earlier would resurrect the empty-window
// hang the splash exists to replace.

const { createSplashTracker, SPLASH_STAGES, SPLASH_MAX_WAIT_MS } = require('../splashWindow.cjs');

describe('splashWindow tracker', () => {
  test('stage order is shell -> interface -> engine -> ready', () => {
    expect(SPLASH_STAGES).toEqual(['shell', 'interface', 'engine', 'ready']);
  });

  test('starts at interface stage, not revealed', () => {
    const t = createSplashTracker();
    expect(t.stage()).toBe('interface');
    expect(t.shouldReveal()).toBe(false);
  });

  test('renderer settling before the interface loads does not reveal early', () => {
    const t = createSplashTracker();
    t.markRendererSettled();
    expect(t.shouldReveal()).toBe(false);
    t.markInterfaceLoaded();
    expect(t.shouldReveal()).toBe(true);
    expect(t.stage()).toBe('ready');
  });

  test('interface loaded + engine still starting stays on the engine stage', () => {
    const t = createSplashTracker();
    t.markInterfaceLoaded();
    t.setEngineState('started');
    // 'started' alone is not the gate latch (config + providers
    // may still be in flight) — the splash stays until the
    // renderer settles.
    expect(t.stage()).toBe('engine');
    expect(t.shouldReveal()).toBe(false);
    t.markRendererSettled();
    expect(t.shouldReveal()).toBe(true);
  });

  test('an engine failure reveals once the interface loaded, even without the renderer signal', () => {
    const t = createSplashTracker();
    t.setEngineState('failed');
    expect(t.shouldReveal()).toBe(false);
    t.markInterfaceLoaded();
    expect(t.shouldReveal()).toBe(true);
    expect(t.stage()).toBe('ready');
  });

  test('forceReveal reveals unconditionally (fallback budget)', () => {
    const t = createSplashTracker();
    t.forceReveal();
    expect(t.shouldReveal()).toBe(true);
    expect(t.stage()).toBe('ready');
  });

  test('unknown engine states are ignored', () => {
    const t = createSplashTracker();
    t.setEngineState('bogus');
    expect(t.snapshot().engineState).toBe('starting');
  });

  test('the fallback budget mirrors the 120s health budget', () => {
    expect(SPLASH_MAX_WAIT_MS).toBe(120000);
  });
});
