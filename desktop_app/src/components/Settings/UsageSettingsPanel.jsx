import React, { useState, useEffect } from 'react';
import { AlertTriangle, Check } from 'lucide-react';
import { formatUsd } from '../CostHeaderPopover';

// Epic 8, E8-A8.1b: Usage & Budget Settings Panel.
//
// Shows ledger totals by model and agent, honest status for local ($0.00)
// and unpriced models, and a soft daily budget alert control that never
// interrupts running tasks.

export default function UsageSettingsPanel({
  wsRef,
  initialTotals = null,
  todaySpend = 0,
  dailyBudget = null,
  budgetExceeded = false,
}) {
  const [totals, setTotals] = useState(initialTotals);
  const [budgetInput, setBudgetInput] = useState(dailyBudget != null ? String(dailyBudget) : '');
  const [savedSuccess, setSavedSuccess] = useState(false);
  const [isExceeded, setIsExceeded] = useState(budgetExceeded);
  const [currentTodaySpend, setCurrentTodaySpend] = useState(todaySpend);

  useEffect(() => {
    if (initialTotals) {
      setTotals(initialTotals);
    }
    if (dailyBudget != null) {
      setBudgetInput(String(dailyBudget));
    }
    setIsExceeded(budgetExceeded || (dailyBudget != null && todaySpend > dailyBudget));
    setCurrentTodaySpend(todaySpend);
  }, [initialTotals, dailyBudget, todaySpend, budgetExceeded]);

  useEffect(() => {
    // If not passed initialTotals or when mounting, query ledger
    if (!initialTotals && wsRef?.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'USAGE_LEDGER_QUERY_REQUEST',
        schema_version: 1,
        request_id: `usage_settings_${Date.now()}`,
        payload: {},
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
  }, [wsRef, initialTotals]);

  const handleSaveBudget = (e) => {
    e.preventDefault();
    const parsed = parseFloat(budgetInput);
    const validBudget = !isNaN(parsed) && parsed > 0 ? parsed : null;

    if (wsRef?.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'DAILY_BUDGET_SET_REQUEST',
        schema_version: 1,
        request_id: `budget_set_${Date.now()}`,
        payload: { daily_budget_usd: validBudget },
      }));
    }
  };

  const byModel = totals?.by_model || {};
  const byAgent = totals?.by_agent || {};
  const totalCost = totals?.total_cost_usd || 0;
  const totalIn = totals?.input_tokens || 0;
  const totalOut = totals?.output_tokens || 0;

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
          {budgetInput && (
            <button
              type="button"
              onClick={() => {
                setBudgetInput('');
                if (wsRef?.current?.readyState === WebSocket.OPEN) {
                  wsRef.current.send(JSON.stringify({
                    type: 'DAILY_BUDGET_SET_REQUEST',
                    schema_version: 1,
                    request_id: `budget_clear_${Date.now()}`,
                    payload: { daily_budget_usd: null },
                  }));
                }
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
