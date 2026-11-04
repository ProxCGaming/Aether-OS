import React from 'react';

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
  testId,
  variant = 'box',
  style,
}) {
  const display = loadDisplay(loadState, isConnected);
  if (display === null) return null;
  const text = display === 'error' ? errorText : display === 'offline' ? offlineText : loadingText;
  if (!text) return null;
  const base = variant === 'plain'
    ? (display === 'error' ? plainError : plainBase)
    : (display === 'error' ? boxError : boxBase);
  return (
    <div data-testid={testId} data-load-display={display} style={{ ...base, ...style }}>
      {text}
    </div>
  );
}
