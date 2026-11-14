import React, { useState, useEffect } from 'react';
import { Gauge } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

// Master build S21 (R17 wiring): the model capability checks —
// the scheduler in aether_engine/scheduler/capability_jobs.py
// that benchmarks and live-self-tests the configured models on
// a schedule. Its three socket events (CAPABILITY_CHECK_SET_
// SCHEDULE / RUN_NOW / GET_HISTORY, all answered by
// CAPABILITY_CHECK_HISTORY_RESPONSE) worked engine-side with
// no UI sender; this card is that sender. It is NOT the
// environment diagnostics card above it (probes of the machine
// and network) and NOT the "This machine" card below it
// (hardware) — the copy says so, because three different
// "capability" answers live on this tab.
//
// Self-contained like MachineCapabilityCard: it asks for the
// history (which carries the status summary) on mount and on
// reconnect, and renders the engine's summary and history
// verbatim — schedule, method, last/next run, per-run results
// and error logs are the scheduler's own records.

const NAMED_SCHEDULES = [
  { value: 'none', label: 'Off (no scheduled runs)' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'custom', label: 'Custom cron…' },
];

const METHODS = [
  { value: 'Option A', label: 'Benchmarks only (Option A)' },
  { value: 'Option C', label: 'Live self-tests only (Option C)' },
  { value: 'Both', label: 'Benchmarks + live self-tests (Both)' },
];

const selectStyle = {
  background: 'var(--bg-base)',
  border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-md)',
  padding: '8px 10px',
  color: 'var(--text-primary)',
  fontSize: '13px',
};

const STATUS_COLORS = {
  SUCCESS: 'var(--success)',
  COMPLETED: 'var(--success)',
  FAILED: 'var(--danger)',
  RUNNING: 'var(--accent)',
};

function formatWhen(epochSeconds) {
  if (!epochSeconds) return 'never';
  return new Date(epochSeconds * 1000).toLocaleString();
}

export default function CapabilityCheckCard() {
  const { wsRef, sendMessage, isConnected, reconnectCount = 0 } = useAetherSocket();
  const [summary, setSummary] = useState(null);
  const [history, setHistory] = useState([]);
  const [latestRun, setLatestRun] = useState(null);
  const [running, setRunning] = useState(false);
  // The method the controls would send — seeded from the
  // engine's summary when it lands, then owned by the user.
  const [method, setMethod] = useState('Both');
  const [cronText, setCronText] = useState('');
  const [cronDirty, setCronDirty] = useState(false);
  // The schedule the user last picked, until the engine's next
  // summary lands and governs again: a method change made right
  // after a schedule change must send the schedule on screen,
  // not the stale one in the last summary.
  const [scheduleChoice, setScheduleChoice] = useState(null);

  useEffect(() => {
    sendMessage('CAPABILITY_CHECK_GET_HISTORY', { schema_version: 1, request_id: Date.now().toString(), payload: {} });

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'CAPABILITY_CHECK_HISTORY_RESPONSE') {
          const payload = data.payload || {};
          if (payload.summary) {
            setSummary(payload.summary);
            setScheduleChoice(null);
            if (payload.summary.method) setMethod(payload.summary.method);
            if (!cronDirty && payload.summary.schedule
              && !NAMED_SCHEDULES.some((s) => s.value === payload.summary.schedule)) {
              setCronText(payload.summary.schedule);
            }
          }
          if (Array.isArray(payload.history)) setHistory(payload.history);
          if (payload.latest_run) {
            setLatestRun(payload.latest_run);
            setRunning(false);
          }
        }
      } catch (e) {
        // Not JSON / not ours — other panels own their replies.
      }
    };

    if (wsRef.current) {
      wsRef.current.addEventListener('message', handleMessage);
    }
    return () => {
      if (wsRef.current) {
        wsRef.current.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef, sendMessage, reconnectCount]); // eslint-disable-line react-hooks/exhaustive-deps

  const scheduleValue = scheduleChoice ?? (!summary
    ? 'weekly'
    : NAMED_SCHEDULES.some((s) => s.value === summary.schedule && s.value !== 'custom')
      ? summary.schedule
      : 'custom');

  const sendSchedule = (schedule, nextMethod) => {
    sendMessage('CAPABILITY_CHECK_SET_SCHEDULE', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { schedule, method: nextMethod },
    });
  };

  const handleScheduleChange = (value) => {
    setScheduleChoice(value);
    if (value === 'custom') {
      // Reveal the cron field seeded with the current value;
      // nothing is sent until the user applies it.
      setCronText(summary && scheduleValue === 'custom' ? summary.schedule : (cronText || '0 3 * * 1'));
      setCronDirty(true);
      return;
    }
    sendSchedule(value, method);
  };

  const handleApplyCron = () => {
    const value = cronText.trim();
    if (!value) return;
    setCronDirty(false);
    setScheduleChoice('custom');
    sendSchedule(value, method);
  };

  const handleRunNow = () => {
    setRunning(true);
    setLatestRun(null);
    sendMessage('CAPABILITY_CHECK_RUN_NOW', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { method },
    });
  };

  const renderRun = (run, testIdPrefix) => (
    <div
      key={run.id || `${testIdPrefix}-${run.started_at}`}
      data-testid={`${testIdPrefix}-${run.id || run.started_at}`}
      style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '12px 16px' }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '13px', fontWeight: 500 }}>{formatWhen(run.started_at)}</span>
        <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{run.method}</span>
        <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
          {run.models_tested} model{run.models_tested === 1 ? '' : 's'} tested
        </span>
        <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
          {typeof run.cost_estimate === 'number' ? `$${run.cost_estimate.toFixed(2)}` : ''}
        </span>
        <span
          style={{
            fontSize: '11px', fontWeight: 600, color: STATUS_COLORS[run.status] || 'var(--text-muted)',
            border: '1px solid var(--border-default)', borderRadius: '999px', padding: '2px 10px', whiteSpace: 'nowrap',
          }}
        >
          {run.status}
        </span>
      </div>
      {run.error_log && (
        <div style={{ fontSize: '12px', color: 'var(--danger)', marginTop: '6px', whiteSpace: 'pre-wrap' }}>
          {run.error_log}
        </div>
      )}
    </div>
  );

  return (
    <div
      data-testid="capability-check-card"
      style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '24px', marginTop: '20px' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
        <Gauge size={18} style={{ color: 'var(--accent)' }} />
        <h3 style={{ fontSize: '16px', margin: 0 }}>Model capability checks</h3>
      </div>
      <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: '0 0 16px 0' }}>
        Scheduled checks of the models themselves — their recorded benchmarks and, with live
        self-tests, a real prompt run against every configured model (which spends real API
        calls). This is separate from the diagnostics run above and the hardware card below.
      </p>

      {!summary ? (
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          {isConnected
            ? 'Asking the engine for the check schedule and history…'
            : 'Waiting for the engine — the check schedule and history will appear here once it connects.'}
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: '16px' }}>
            <div>
              <label htmlFor="capability-check-schedule" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Schedule</label>
              <select
                id="capability-check-schedule"
                data-testid="capability-check-schedule"
                value={scheduleValue}
                onChange={(e) => handleScheduleChange(e.target.value)}
                style={selectStyle}
              >
                {NAMED_SCHEDULES.map((s) => (
                  <option key={s.value} value={s.value}>{s.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="capability-check-method" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Method</label>
              <select
                id="capability-check-method"
                data-testid="capability-check-method"
                value={method}
                onChange={(e) => {
                  setMethod(e.target.value);
                  if (scheduleValue !== 'custom') sendSchedule(scheduleValue, e.target.value);
                }}
                style={selectStyle}
              >
                {METHODS.map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </div>
            <button
              data-testid="capability-check-run-now"
              onClick={handleRunNow}
              disabled={running || !isConnected}
              style={{
                background: 'transparent', color: 'var(--accent)',
                border: '1px solid rgba(var(--accent-rgb), 0.5)',
                padding: '8px 16px', borderRadius: '8px',
                cursor: running ? 'wait' : 'pointer', fontSize: '13px',
              }}
            >
              {running ? 'Running…' : 'Run check now'}
            </button>
          </div>

          {scheduleValue === 'custom' && (
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '16px' }}>
              <input
                data-testid="capability-check-cron"
                type="text"
                value={cronText}
                onChange={(e) => { setCronText(e.target.value); setCronDirty(true); }}
                placeholder="5-field cron, e.g. 0 3 * * 1"
                spellCheck={false}
                style={{ ...selectStyle, flex: 1, fontFamily: 'var(--font-mono)' }}
              />
              <button
                data-testid="capability-check-cron-apply"
                onClick={handleApplyCron}
                style={{
                  background: 'rgba(var(--accent-rgb),0.15)', color: 'var(--accent)',
                  border: '1px solid rgba(var(--accent-rgb),0.4)',
                  padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 500,
                }}
              >
                Apply cron
              </button>
            </div>
          )}

          {summary.schedule_error && (
            <div data-testid="capability-check-schedule-error" style={{ fontSize: '13px', color: 'var(--danger)', marginBottom: '16px' }}>
              The engine could not honour that schedule: {summary.schedule_error}
            </div>
          )}

          <div data-testid="capability-check-status" style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '16px', lineHeight: 1.6 }}>
            <div>Last run: {summary.last_run ? `${formatWhen(summary.last_run.finished_at || summary.last_run.started_at)} — ${summary.last_run.status}` : 'never'}</div>
            <div>
              {summary.schedule === 'none'
                ? 'No scheduled runs.'
                : summary.next_run_ts
                  ? `Next scheduled run: ${formatWhen(summary.next_run_ts)}`
                  : 'Next run: the engine reports no upcoming run.'}
            </div>
            {running && <div>A check is running — it tests every configured model and can take minutes; the result lands here when it finishes.</div>}
          </div>

          {latestRun && (
            <div style={{ marginBottom: '16px' }}>
              <div style={{ fontSize: '13px', fontWeight: 500, marginBottom: '8px' }}>Latest run</div>
              {renderRun(latestRun, 'capability-check-latest')}
            </div>
          )}

          <div>
            <div style={{ fontSize: '13px', fontWeight: 500, marginBottom: '8px' }}>History</div>
            {history.length === 0 ? (
              <div data-testid="capability-check-history-empty" style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                No capability checks have run yet.
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {history.map((run) => renderRun(run, 'capability-check-run'))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
