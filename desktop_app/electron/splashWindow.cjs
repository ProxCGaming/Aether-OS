'use strict';

/**
 * Native splash window — stage tracking and the reveal rule.
 *
 * Owner request (23 Nov 2026): a native splash shows the loading
 * animation from the first frame while the app gets ready in the
 * background, replacing the sequence where an empty transparent
 * main window hung on screen first and the renderer's StartupGate
 * ("Starting the engine…") only appeared after first paint.
 *
 * This module is the pure decision logic, in the style of
 * wsReconnect.cjs / snipGeometry.cjs — no Electron import, so Jest
 * drives it directly. main.cjs owns the windows: it creates the
 * splash first, creates the main window HIDDEN, feeds this tracker
 * the real events, mirrors the stage into the splash page, and
 * reveals the main window (closing the splash) exactly when
 * shouldReveal() turns true.
 *
 * The stages are real events, never a fake percentage (the same
 * honesty rule StartupGate follows — there is no honest % for
 * module loading or engine health):
 *
 *   shell     — main is alive; the splash itself is the proof.
 *   interface — the main window exists; its renderer is loading.
 *   engine    — the renderer finished loading; the engine start
 *               is still settling (lifecycle state 'starting').
 *   ready     — reveal: the renderer's startup gate has latched
 *               ready OR reported failure (its failure screen
 *               carries Retry / Open-log, so it must be visible),
 *               or the fallback budget elapsed.
 *
 * Reveal requires the interface to have loaded AND one of:
 *   - the renderer signalled its gate settled (ready or failed —
 *     App.jsx sends one 'startup-ready' IPC for both), or
 *   - the engine lifecycle reported 'failed' (covers a renderer
 *     that loaded but whose gate signal never arrives), or
 *   - forceReveal() after SPLASH_MAX_WAIT_MS — the engine's full
 *     120 s health budget, mirroring StartupGate's S-10
 *     escalation: nobody stays on a splash forever; the gate's
 *     recovery view takes over from there.
 */

// The engine's full health budget (StartupGate's
// STARTUP_ESCALATION_MS): past this, reveal whatever the state,
// so the user lands on the gate's recovery actions instead of an
// endless splash.
const SPLASH_MAX_WAIT_MS = 120000;

const SPLASH_STAGES = ['shell', 'interface', 'engine', 'ready'];

function createSplashTracker() {
  let interfaceLoaded = false;
  let engineState = 'starting'; // 'starting' | 'started' | 'failed'
  let rendererSettled = false;
  let forced = false;

  const stage = () => {
    if (shouldReveal()) return 'ready';
    if (!interfaceLoaded) return 'interface';
    return 'engine';
  };

  const shouldReveal = () => {
    if (forced) return true;
    if (!interfaceLoaded) return false;
    return rendererSettled || engineState === 'failed';
  };

  return {
    /** The main window's renderer finished (or failed) loading. */
    markInterfaceLoaded() {
      interfaceLoaded = true;
    },
    /** Mirror of main's engine startup state. */
    setEngineState(state) {
      if (state === 'starting' || state === 'started' || state === 'failed') {
        engineState = state;
      }
    },
    /** The renderer's startup gate latched ready or failed. */
    markRendererSettled() {
      rendererSettled = true;
    },
    /** The fallback budget elapsed — reveal unconditionally. */
    forceReveal() {
      forced = true;
    },
    stage,
    shouldReveal,
    snapshot() {
      return { interfaceLoaded, engineState, rendererSettled, forced, stage: stage(), shouldReveal: shouldReveal() };
    },
  };
}

module.exports = { createSplashTracker, SPLASH_STAGES, SPLASH_MAX_WAIT_MS };
