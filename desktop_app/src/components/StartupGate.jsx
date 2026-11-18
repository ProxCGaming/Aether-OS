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
 * legitimately take the full 120s health budget. Elapsed time past
 * that budget escalates the starting screen to a recovery view
 * (S-10): Retry + Open engine log are offered while the engine may
 * still come up — the phase stays 'starting' and nothing declares
 * failure. Once ready has been reached the gate never returns;
 * mid-session drops belong to the shell's existing offline handling.
 *
 * Styling reuses the app's own idioms: the shell's var(--bg-base)
 * ground, Onboarding's panel + buttons, the .aether-spinner from
 * index.css, and Aether's amber (var(--accent)) as the accent. No
 * progress bar — there is no honest percentage to show.
 */

// After this long in 'starting', add the reassurance line. Deliberately
// far below the 120s health budget: it calms, it never declares failure.
export const STARTUP_SLOW_NOTICE_MS = 20000;

// After the FULL health budget in 'starting' (S-10), escalate to the
// recovery view: the same Retry / Open-log actions the failed screen
// offers, while the phase stays 'starting' — an engine that started
// but wedged before answering must not leave the user on an endless
// spinner with no way out but killing the app (FF1-16).
export const STARTUP_ESCALATION_MS = 120000;

// The gate's phase from App's signals. Pure, so the state machine is
// unit-testable without rendering App.
export function deriveStartupPhase({ passed = false, ready = false, engineState = null } = {}) {
  if (passed || ready) return 'ready';
  if (engineState === 'failed') return 'failed';
  return 'starting';
}

const panelStyle = {
  background: 'var(--bg-raised)', border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-lg)', padding: '32px', width: '440px', maxWidth: '92vw', color: 'var(--text-primary)',
  textAlign: 'center',
};

const primaryBtn = {
  background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', padding: '10px 22px',
  borderRadius: 'var(--radius-lg)', cursor: 'pointer', fontWeight: 600,
};

const ghostBtn = {
  background: 'transparent', color: 'var(--text-secondary)', border: 'none', padding: '10px 14px',
  cursor: 'pointer', fontSize: '13px',
};

export default function StartupGate({ phase, onRetry, onOpenLog }) {
  const [slowNotice, setSlowNotice] = useState(false);
  const [escalated, setEscalated] = useState(false);

  // The slow-start reassurance and the S-10 escalation are functions
  // of THIS starting spell: entering 'starting' (first mount, or back
  // from a Retry) resets both.
  useEffect(() => {
    if (phase !== 'starting') return undefined;
    setSlowNotice(false);
    setEscalated(false);
    const slowTimer = setTimeout(() => setSlowNotice(true), STARTUP_SLOW_NOTICE_MS);
    const escalationTimer = setTimeout(() => setEscalated(true), STARTUP_ESCALATION_MS);
    return () => { clearTimeout(slowTimer); clearTimeout(escalationTimer); };
  }, [phase]);

  const failed = phase === 'failed';

  return (
    <div
      data-testid="startup-gate"
      style={{
        position: 'fixed', inset: 0, background: 'var(--bg-base)', zIndex: 300,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div style={panelStyle}>
        <div style={{ fontSize: '12px', color: 'var(--accent)', letterSpacing: '1px', textTransform: 'uppercase', marginBottom: '8px' }}>
          Aether-OS
        </div>
        {failed ? (
          <>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>The engine couldn&rsquo;t start</h2>
            <p style={{ color: 'var(--text-secondary)', fontSize: '14px', marginTop: 0 }}>
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
                  border: '3px solid rgba(var(--accent-rgb), 0.25)', borderTopColor: 'var(--accent)',
                }}
              />
            </div>
            <h2 style={{ margin: '0 0 8px 0', fontSize: '22px' }}>Starting the engine…</h2>
            <p style={{ color: 'var(--text-secondary)', fontSize: '14px', marginTop: 0 }}>
              Aether is getting everything ready. This screen moves on by itself once the engine is healthy.
            </p>
            {slowNotice && !escalated && (
              <p style={{ color: 'var(--accent)', fontSize: '13px', marginBottom: 0 }}>
                Cold starts can take a while — the engine is given up to two minutes to come up.
              </p>
            )}
            {escalated && (
              <>
                <p style={{ color: 'var(--accent)', fontSize: '13px', marginBottom: 0 }}>
                  This is taking longer than the engine&rsquo;s full two-minute health budget.
                  It may still come up on its own — or you can retry the start, or open the
                  engine log to see what&rsquo;s happening.
                </p>
                <div style={{ display: 'flex', justifyContent: 'center', gap: '8px', marginTop: '20px' }}>
                  <button type="button" style={ghostBtn} onClick={onOpenLog}>Open engine log</button>
                  <button type="button" style={primaryBtn} onClick={onRetry}>Retry</button>
                </div>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
