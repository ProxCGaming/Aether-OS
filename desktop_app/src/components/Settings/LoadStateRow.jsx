import React, { useEffect, useState } from 'react';

// Wave 3 slice 2 (mechanism 5 — no in-flight state may wait
// forever in silence): after this long in a loading/offline state,
// the row gains a "taking longer than usual" line. The value
// matches the StartupGate's slow notice (STARTUP_SLOW_NOTICE_MS):
// long enough to calm, never a declaration of failure — the load
// state itself does not change, and no fake data is substituted.
export const LOAD_SLOW_NOTICE_MS = 20000;

export const DEFAULT_SLOW_TEXT = 'This is taking longer than usual — the engine may be busy or still starting.';

// True once `active` has been continuously true for
// LOAD_SLOW_NOTICE_MS; resets the moment it goes false.
export function useSlowLoad(active) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) {
      setSlow(false);
      return undefined;
    }
    const timer = setTimeout(() => setSlow(true), LOAD_SLOW_NOTICE_MS);
    return () => clearTimeout(timer);
  }, [active]);
  return slow;
}

// Wave 3 slice 1 (RUX-C-01…C-05): the shared load-state idiom for
// engine-backed surfaces. Every such surface tracks ONE load state
// per data set it renders:
//
//   'loading' — the request is in flight; no answer yet.
//   'ready'   — an answer arrived; an empty list now means the
//               engine genuinely reported nothing.
//   'error'   — the request failed / the reply said so.
//
// 'offline' is not a fourth stored state: it is how 'loading'
// renders when the socket is known to be down (isConnected ===
// false) — a waiting-for-the-engine state, never the empty state.
// The distinction this component exists to protect is "not yet
// answered" vs "answered empty": before Wave 3, the initial React
// state (an empty list, $0.00, a factory preset) was rendered as if
// it were the engine's answer, so loading, empty, failed and
// offline all looked identical — usually like a confident empty.
//
// Usage: render <LoadStateRow … /> INSTEAD of the surface's content
// or empty state while loadState !== 'ready'; it renders nothing
// once the surface is ready. Copy follows one shape everywhere:
// loading names what is being fetched, offline starts "Waiting for
// the engine —", error names what could not be loaded.
export function loadDisplay(loadState, isConnected) {
  if (loadState === 'ready') return null;
  if (loadState === 'error') return 'error';
  return isConnected === false ? 'offline' : 'loading';
}

const boxBase = {
  color: '#9090a0',
  fontSize: '14px',
  padding: '24px',
  textAlign: 'center',
  background: 'rgba(0,0,0,0.2)',
  borderRadius: '12px',
};

const boxError = {
  color: '#fca5a5',
  fontSize: '13px',
  padding: '16px',
  textAlign: 'center',
  background: 'rgba(239,68,68,0.08)',
  border: '1px solid rgba(239,68,68,0.3)',
  borderRadius: '12px',
};

const plainBase = { color: '#9090a0', fontSize: '13px' };
const plainError = { color: '#fca5a5', fontSize: '13px' };

export default function LoadStateRow({
  loadState,
  isConnected = null,
  loadingText,
  offlineText,
  errorText,
  // Slow-notice line for a load that outlasts LOAD_SLOW_NOTICE_MS:
  // omit for the shared default copy, pass a string to customise,
  // or null to opt the surface out. Errors never gain the line —
  // they have already resolved, just badly.
  slowText,
  testId,
  variant = 'box',
  style,
}) {
  const display = loadDisplay(loadState, isConnected);
  const slow = useSlowLoad(display === 'loading' || display === 'offline');
  if (display === null) return null;
  const text = display === 'error' ? errorText : display === 'offline' ? offlineText : loadingText;
  if (!text) return null;
  const base = variant === 'plain'
    ? (display === 'error' ? plainError : plainBase)
    : (display === 'error' ? boxError : boxBase);
  const showSlow = slow && slowText !== null;
  return (
    <div data-testid={testId} data-load-display={display} style={{ ...base, ...style }}>
      {text}
      {showSlow && (
        <div data-testid={testId ? `${testId}-slow` : undefined} style={{ marginTop: '6px' }}>
          {slowText || DEFAULT_SLOW_TEXT}
        </div>
      )}
    </div>
  );
}
