import React, { useState, useEffect } from 'react';
import { AlertTriangle, Check } from 'lucide-react';
import { formatUsd } from '../CostHeaderPopover';
import { COMPONENT_COLORS, componentSharePct, contextCapacity, contextShareBasis } from '../ContextBar';

// Epic 8, E8-A8.1b: Usage & Budget Settings Panel.
//
// Shows ledger totals by model and agent, honest status for local ($0.00)
// and unpriced models, and a soft daily budget alert control that never
// interrupts running tasks.
//
// N14: the panel also shows the CURRENT working model's context —
// used vs capacity, per component — from the same CONTEXT_BREAKDOWN
// snapshot the session header consumes (no separate data source).

export default function UsageSettingsPanel({
  wsRef,
  safeSend = null,
  pendingMessages = [],
  reconnectCount = 0,
  initialTotals = null,
  initialBreakdown = null,
  sessionId = null,
  todaySpend = 0,
  dailyBudget = null,
  budgetExceeded = false,
}) {
  const [totals, setTotals] = useState(initialTotals);
  const [breakdown, setBreakdown] = useState(initialBreakdown);
  const [budgetInput, setBudgetInput] = useState(dailyBudget != null ? String(dailyBudget) : '');
  const [savedSuccess, setSavedSuccess] = useState(false);
  const [isExceeded, setIsExceeded] = useState(budgetExceeded);
  const [currentTodaySpend, setCurrentTodaySpend] = useState(todaySpend);

  useEffect(() => {
    if (initialTotals) {
      setTotals(initialTotals);
    }
    if (initialBreakdown) {
      setBreakdown(initialBreakdown);
    }
    if (dailyBudget != null) {
      setBudgetInput(String(dailyBudget));
    }
    setIsExceeded(budgetExceeded || (dailyBudget != null && todaySpend > dailyBudget));
    setCurrentTodaySpend(todaySpend);
  }, [initialTotals, initialBreakdown, dailyBudget, todaySpend, budgetExceeded]);

  useEffect(() => {
    // If not passed initialTotals or when mounting, query ledger.
    // Wave 2 slice 4 (RUX-A-17): both queries below are OPEN-guarded,
    // so mounting while the engine was down left the panel rendering
    // its null state ($0.00 / 0 tokens) with no recovery; the
    // reconnectCount dep re-runs this effect on every reconnect.
    if (!initialTotals && wsRef?.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'USAGE_LEDGER_QUERY_REQUEST',
        schema_version: 1,
        request_id: `usage_settings_${Date.now()}`,
        payload: {},
      }));
    }

    // N14: fetch the current context snapshot over the same surface
    // the session header uses; the engine defaults the model to the
    // active working model when none is named.
    if (!initialBreakdown && wsRef?.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'CONTEXT_BREAKDOWN_REQUEST',
        schema_version: 1,
        request_id: `usage_context_${Date.now()}`,
        payload: { session_id: sessionId || '', task_id: null, model: null },
      }));
    }

    if (!wsRef?.current) return;

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'USAGE_LEDGER_QUERY_RESPONSE') {
          if (data.payload?.totals) {
            setTotals(data.payload.totals);
          }
        } else if (data.type === 'CONTEXT_BREAKDOWN_RESPONSE') {
          if (data.payload) {
            setBreakdown(data.payload);
          }
        } else if (data.type === 'DAILY_BUDGET_SET_RESPONSE') {
          const b = data.payload?.daily_budget_usd;
          setBudgetInput(b != null ? String(b) : '');
          setSavedSuccess(true);
          setTimeout(() => setSavedSuccess(false), 2500);
        } else if (data.type === 'DAILY_BUDGET_EXCEEDED') {
          setIsExceeded(true);
          if (data.payload?.today_spend_usd != null) {
            setCurrentTodaySpend(data.payload.today_spend_usd);
          }
        }
      } catch (_e) {
        // Ignored
      }
    };

    const ws = wsRef.current;
    if (ws?.addEventListener) {
      ws.addEventListener('message', handleMessage);
    }
    return () => {
      if (ws?.removeEventListener) {
        ws.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef, initialTotals, initialBreakdown, sessionId, reconnectCount]);

  // RUX-A-20: Save / Clear are user actions on a money-related
  // setting — through safeSend they QUEUE while the engine is
  // unreachable instead of being discarded by the readyState guard
  // this used to carry (the click produced neither success nor
  // error, and "Saved" only ever appeared on an engine response).
  // The standalone-mount fallback keeps the exact legacy envelope,
  // request_id prefixes included. While a change is queued, the
  // controls below are inert and the note names what will land —
  // the "Saved" confirmation still comes only from the response.
  const sendBudgetSet = (value) => {
    if (safeSend) return safeSend('DAILY_BUDGET_SET_REQUEST', { daily_budget_usd: value });
    if (wsRef?.current) {
      wsRef.current.send(JSON.stringify({
        type: 'DAILY_BUDGET_SET_REQUEST',
        schema_version: 1,
        request_id: `${value === null ? 'budget_clear' : 'budget_set'}_${Date.now()}`,
        payload: { daily_budget_usd: value },
      }));
    }
    return undefined;
  };
  const pendingBudgetEntry = (pendingMessages || []).find(
    (m) => m && m.type === 'DAILY_BUDGET_SET_REQUEST',
  );

  const handleSaveBudget = (e) => {
    e.preventDefault();
    const parsed = parseFloat(budgetInput);
    const validBudget = !isNaN(parsed) && parsed > 0 ? parsed : null;
    sendBudgetSet(validBudget);
  };

  const byModel = totals?.by_model || {};
  const byAgent = totals?.by_agent || {};
  const totalCost = totals?.total_cost_usd || 0;
  const totalIn = totals?.input_tokens || 0;
  const totalOut = totals?.output_tokens || 0;

  // N14: current-context derivations share the header's basis logic —
  // capacity-relative whenever the snapshot carries the model's limit.
  const breakdownCapacity = contextCapacity(breakdown);
  const breakdownBasis = breakdown ? contextShareBasis(breakdown).basis : 'capacity';
  const breakdownComponents = breakdown?.components || [];

  return (
    <div data-testid="usage-settings-panel" style={{ color: '#f0f0f5', display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Usage & Budget</h2>
        <p style={{ color: '#9090a0', fontSize: '14px' }}>
          Historical token usage and cost metrics across all tasks, and daily budget alerting.
        </p>
      </div>

      {/* Daily Budget Soft Alert Banner */}
      {isExceeded && (
        <div
          data-testid="budget-exceeded-banner"
          style={{
            background: 'rgba(245, 158, 11, 0.1)',
            border: '1px solid rgba(245, 158, 11, 0.3)',
            borderRadius: '12px',
            padding: '16px',
            display: 'flex',
            alignItems: 'center',
            gap: '12px',
            color: '#fbbf24',
          }}
        >
          <AlertTriangle size={20} />
          <div>
            <div style={{ fontWeight: 600, fontSize: '14px' }}>
              Daily Budget Exceeded ({formatUsd(currentTodaySpend)} spent today)
            </div>
            <div style={{ fontSize: '12px', color: 'rgba(255, 255, 255, 0.7)', marginTop: '2px' }}>
              Running tasks finish uninterrupted. Budget limits are soft alerts and never halt work mid-flight.
            </div>
          </div>
        </div>
      )}

      {/* Budget Control Card */}
      <div
        style={{
          background: 'rgba(255, 255, 255, 0.02)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          borderRadius: '12px',
          padding: '20px',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
          <div>
            <h3 style={{ fontSize: '16px', fontWeight: 500, color: '#fff', marginBottom: '4px' }}>
              Daily Spending Alert
            </h3>
            <p style={{ fontSize: '13px', color: '#9090a0' }}>
              Set an optional soft threshold in USD. You will be notified when exceeded.
            </p>
          </div>
          <div style={{ textAlign: 'right' }}>
            <span style={{ fontSize: '12px', color: '#9090a0' }}>Today's spend</span>
            <div style={{ fontSize: '16px', fontWeight: 600, color: '#fff' }}>
              {formatUsd(currentTodaySpend)}
            </div>
          </div>
        </div>

        <form onSubmit={handleSaveBudget} style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
          <div style={{ position: 'relative', width: '220px' }}>
            <span style={{ position: 'absolute', left: '12px', top: '10px', color: '#9090a0', fontSize: '14px' }}>$</span>
            <input
              type="number"
              step="0.01"
              min="0"
              data-testid="daily-budget-input"
              placeholder="e.g. 5.00"
              value={budgetInput}
              onChange={(e) => setBudgetInput(e.target.value)}
              style={{
                width: '100%',
                padding: '9px 12px 9px 26px',
                borderRadius: '8px',
                background: 'rgba(255, 255, 255, 0.05)',
                border: '1px solid rgba(255, 255, 255, 0.12)',
                color: '#fff',
                fontSize: '13px',
                outline: 'none',
              }}
            />
          </div>
          <button
            type="submit"
            data-testid="daily-budget-save-btn"
            disabled={Boolean(pendingBudgetEntry)}
            style={{
              padding: '9px 18px',
              borderRadius: '8px',
              background: '#3b82f6',
              border: 'none',
              color: '#fff',
              fontSize: '13px',
              fontWeight: 500,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            {savedSuccess ? <><Check size={14} /> Saved</> : 'Save Budget'}
          </button>
          {pendingBudgetEntry && (
            <span data-testid="daily-budget-queued" style={{ fontSize: '12px', color: '#fbbf24' }}>
              {pendingBudgetEntry.payload && pendingBudgetEntry.payload.daily_budget_usd != null
                ? `Daily budget change to $${pendingBudgetEntry.payload.daily_budget_usd} queued — will apply when the engine reconnects.`
                : 'Daily budget clear queued — will apply when the engine reconnects.'}
            </span>
          )}
          {budgetInput && (
            <button
              type="button"
              disabled={Boolean(pendingBudgetEntry)}
              onClick={() => {
                setBudgetInput('');
                sendBudgetSet(null);
              }}
              style={{
                background: 'transparent',
                border: 'none',
                color: '#9090a0',
                fontSize: '12px',
                cursor: 'pointer',
                textDecoration: 'underline',
              }}
            >
              Clear
            </button>
          )}
        </form>
      </div>

      {/* Current Context (N14) — the working model right now, from the
          same breakdown snapshot as the session header. */}
      <div
        data-testid="usage-current-context"
        style={{
          background: 'rgba(255, 255, 255, 0.02)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          borderRadius: '12px',
          padding: '20px',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 500, color: '#fff' }}>Current Context</h3>
          {breakdown && (
            <span style={{ fontSize: '13px', color: '#9090a0' }}>
              <strong style={{ color: '#fff' }}>{breakdown.model}</strong>
              {' · '}
              {breakdownCapacity != null
                ? `${(breakdown.total_tokens || 0).toLocaleString()} / ${breakdownCapacity.toLocaleString()} tokens (${breakdown.percent_used != null ? breakdown.percent_used : 0}%)`
                : `${(breakdown.total_tokens || 0).toLocaleString()} tokens · window size unknown`}
            </span>
          )}
        </div>

        {!breakdown ? (
          <div style={{ color: '#9090a0', fontSize: '13px', padding: '12px 0' }}>
            No current context snapshot yet — it appears once the engine answers for the active session.
          </div>
        ) : (
          <>
            <div
              data-testid="usage-context-segmented-bar"
              style={{
                width: '100%',
                height: '10px',
                background: 'rgba(255, 255, 255, 0.08)',
                borderRadius: '5px',
                overflow: 'hidden',
                display: 'flex',
                marginBottom: breakdownBasis === 'used' ? '8px' : '16px',
              }}
            >
              {breakdownComponents.map((c) => {
                if (c.unknown === true || c.tokens == null || c.tokens <= 0) return null;
                const slug = c.name.replace(/\s+/g, '-');
                return (
                  <div
                    key={c.name}
                    data-testid={`usage-context-segment-${slug}`}
                    style={{
                      width: `${componentSharePct(c, breakdown)}%`,
                      height: '100%',
                      background: COMPONENT_COLORS[c.name] || '#94a3b8',
                      transition: 'width 0.3s ease',
                    }}
                  />
                );
              })}
            </div>
            {breakdownBasis === 'used' && (
              <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
                Window size unknown — shares are of used tokens, not of the full window.
              </div>
            )}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {breakdownComponents.map((c) => {
                const slug = c.name.replace(/\s+/g, '-');
                // M20 honesty: unmeasured components render as
                // unknown with no share — never a fabricated number.
                const isUnknown = c.unknown === true || c.tokens == null;
                const pct = componentSharePct(c, breakdown);
                return (
                  <div
                    key={c.name}
                    data-testid={`usage-context-item-${slug}`}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: '8px 12px',
                      background: 'rgba(255, 255, 255, 0.03)',
                      borderRadius: '8px',
                      fontSize: '13px',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span
                        style={{
                          width: '8px',
                          height: '8px',
                          borderRadius: '50%',
                          background: !isUnknown && c.tokens > 0 ? (COMPONENT_COLORS[c.name] || '#94a3b8') : 'rgba(255, 255, 255, 0.2)',
                          display: 'inline-block',
                        }}
                      />
                      <span style={{ textTransform: 'capitalize' }}>{c.name}</span>
                    </div>
                    <div style={{ display: 'flex', gap: '16px', alignItems: 'center' }}>
                      <span style={{ color: '#9090a0', fontSize: '12px' }}>
                        {isUnknown ? '—' : `${pct.toFixed(1)}%`}
                      </span>
                      <span style={{ fontWeight: 500, minWidth: '60px', textAlign: 'right' }}>
                        {isUnknown ? 'unknown' : c.tokens.toLocaleString()}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
            {breakdown.partial && (
              <div style={{ fontSize: '12px', color: '#9090a0', marginTop: '12px' }}>
                Some components could not be measured — the total is a lower bound.
              </div>
            )}
          </>
        )}
      </div>

      {/* Usage by Model */}
      <div
        style={{
          background: 'rgba(255, 255, 255, 0.02)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          borderRadius: '12px',
          padding: '20px',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 500, color: '#fff' }}>Usage by Model</h3>
          <span style={{ fontSize: '13px', color: '#9090a0' }}>
            Total: <strong style={{ color: '#fff' }}>{formatUsd(totalCost)}</strong> ({totalIn.toLocaleString()} in / {totalOut.toLocaleString()} out)
          </span>
        </div>

        {Object.keys(byModel).length === 0 ? (
          <div style={{ color: '#9090a0', fontSize: '13px', padding: '12px 0' }}>
            No model calls recorded in the ledger yet.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {Object.entries(byModel).map(([modelName, m]) => (
              <div
                key={modelName}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '10px 12px',
                  background: 'rgba(255, 255, 255, 0.03)',
                  borderRadius: '8px',
                  fontSize: '13px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <span style={{ fontWeight: 500 }}>{modelName}</span>
                  {m.is_local && (
                    <span
                      data-testid={`badge-local-${modelName}`}
                      style={{
                        background: 'rgba(255, 255, 255, 0.1)',
                        color: 'rgba(255, 255, 255, 0.8)',
                        padding: '1px 8px',
                        borderRadius: '999px',
                        fontSize: '11px',
                      }}
                    >
                      local
                    </span>
                  )}
                  {m.unpriced && (
                    <span
                      data-testid={`badge-unpriced-${modelName}`}
                      style={{
                        background: 'rgba(245, 158, 11, 0.15)',
                        color: '#fbbf24',
                        padding: '1px 8px',
                        borderRadius: '999px',
                        fontSize: '11px',
                      }}
                    >
                      unpriced
                    </span>
                  )}
                </div>
                <div style={{ display: 'flex', gap: '16px', alignItems: 'center' }}>
                  <span style={{ color: '#9090a0', fontSize: '12px' }}>
                    {m.calls || 0} calls · {m.input_tokens.toLocaleString()} in · {m.output_tokens.toLocaleString()} out
                  </span>
                  <span style={{ fontWeight: 600, minWidth: '60px', textAlign: 'right' }}>
                    {formatUsd(m.cost_usd)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Usage by Agent */}
      <div
        style={{
          background: 'rgba(255, 255, 255, 0.02)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          borderRadius: '12px',
          padding: '20px',
        }}
      >
        <h3 style={{ fontSize: '16px', fontWeight: 500, color: '#fff', marginBottom: '16px' }}>
          Usage by Agent
        </h3>

        {Object.keys(byAgent).length === 0 ? (
          <div style={{ color: '#9090a0', fontSize: '13px', padding: '12px 0' }}>
            No agent attribution recorded yet.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {Object.entries(byAgent).map(([agentName, a]) => (
              <div
                key={agentName}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '10px 12px',
                  background: 'rgba(255, 255, 255, 0.03)',
                  borderRadius: '8px',
                  fontSize: '13px',
                }}
              >
                <span style={{ fontWeight: 500, textTransform: 'capitalize' }}>{agentName}</span>
                <div style={{ display: 'flex', gap: '16px', alignItems: 'center' }}>
                  <span style={{ color: '#9090a0', fontSize: '12px' }}>
                    {a.calls || 0} calls · {a.input_tokens.toLocaleString()} in · {a.output_tokens.toLocaleString()} out
                  </span>
                  <span style={{ fontWeight: 600, minWidth: '60px', textAlign: 'right' }}>
                    {formatUsd(a.cost_usd)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
