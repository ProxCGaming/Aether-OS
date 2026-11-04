import React, { useEffect, useState } from 'react';

/**
 * Startup gate (Wave 1 slice 4 — the owner's first-launch flow).
 *
 * A full-screen state shown INSTEAD of the app shell until the engine
 * is genuinely ready, so nobody lands in a half-live UI. The state
 * machine is startup-only and lives in App.jsx:
 *
 *   starting → ready | failed → (Retry) → starting
 *
 * "ready" is latched by App the first time the socket is connected
 * AND the initial engine data (config + provider list — the same
 * signals that gate the onboarding overlay's mount) has arrived, and
 * it wins over every other signal: an engine that answers anyway
 * (e.g. an external dev engine) passes the gate even if the bundled
 * lifecycle reported failure. "failed" comes only from main's
 * engine-state channel — the lifecycle's terminal 'none' after its
 * spawn retry — never from elapsed time: a cold start may
 * legitimately take the full 120s health budget. Once ready has been
 * reached the gate never returns; mid-session drops belong to the
 * shell's existing offline handling.
 *
 * Styling reuses the app's own idioms: the shell's #05050f ground,
 * Onboarding's panel + buttons, the .aether-spinner from index.css,
 * and Aether's amber (#f59e0b) as the accent. No progress bar — there
 * is no honest percentage to show.
 */

// After this long in 'starting', add the reassurance line. Deliberately
// far below the 120s health budget: it calms, it never declares failure.
export const STARTUP_SLOW_NOTICE_MS = 20000;

// The gate's phase from App's signals. Pure, so the state machine is
// unit-testable without rendering App.
export function deriveStartupPhase({ passed = false, ready = false, engineState = null } = {}) {
  if (passed || ready) return 'ready';
  if (engineState === 'failed') return 'failed';
  return 'starting';
}

const panelStyle = {
  background: 'rgba(20,20,28,0.97)', border: '1px solid rgba(255,255,255,0.1)',
  borderRadius: '20px', padding: '32px', width: '440px', maxWidth: '92vw', color: '#f0f0f5',
  textAlign: 'center',
};

const primaryBtn = {
  background: '#f59e0b', color: '#1a1205', border: 'none', padding: '10px 22px',
  borderRadius: '12px', cursor: 'pointer', fontWeight: 600,
};

const ghostBtn = {
  background: 'transparent', color: '#9090a0', border: 'none', padding: '10px 14px',
  cursor: 'pointer', fontSize: '13px',
};

export default function StartupGate({ phase, onRetry, onOpenLog }) {
  const [slowNotice, setSlowNotice] = useState(false);

  // The slow-start reassurance is a function of THIS starting spell:
  // entering 'starting' (first mount, or back from a Retry) resets it.
  useEffect(() => {
    if (phase !== 'starting') return undefined;
    setSlowNotice(false);
    const timer = setTimeout(() => setSlowNotice(true), STARTUP_SLOW_NOTICE_MS);
    return () => clearTimeout(timer);
  }, [phase]);

  const failed = phase === 'failed';

  return (
    <div
      data-testid="startup-gate"
      style={{
        position: 'fixed', inset: 0, background: '#05050f', zIndex: 300,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div style={panelStyle}>
        <div style={{ fontSize: '12px', color: '#f59e0b', letterSpacing: '1px', textTransform: 'uppercase', marginBottom: '8px' }}>
          Aether-OS
        </div>
        {failed ? (
          <>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>The engine couldn&rsquo;t start</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginTop: 0 }}>
              Aether&rsquo;s engine didn&rsquo;t come up, so there&rsquo;s nothing to connect to yet.
              You can try starting it again, or open the engine log to see what happened.
            </p>
            <div style={{ display: 'flex', justifyContent: 'center', gap: '8px', marginTop: '20px' }}>
              <button type="button" style={ghostBtn} onClick={onOpenLog}>Open engine log</button>
              <button type="button" style={primaryBtn} onClick={onRetry}>Retry</button>
            </div>
          </>
        ) : (
          <>
            <div style={{ display: 'flex', justifyContent: 'center', margin: '10px 0 18px 0' }}>
              <span
                className="aether-spinner"
                aria-hidden="true"
                style={{
                  width: '26px', height: '26px', borderRadius: '50%', display: 'inline-block',
                  border: '3px solid rgba(245,158,11,0.25)', borderTopColor: '#f59e0b',
                }}
              />
            </div>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>Starting the engine…</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginTop: 0 }}>
              Aether is getting everything ready. This screen moves on by itself once the engine is healthy.
            </p>
            {slowNotice && (
              <p style={{ color: '#f59e0b', fontSize: '13px', marginBottom: 0 }}>
                Cold starts can take a while — the engine is given up to two minutes to come up.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
