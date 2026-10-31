import React from 'react';

// Epic 8, E8-A8.1a: live session-cost popover for the chat header.
//
// The figures rendered here are the usage ledger's own: App builds
// `tasks` either from USAGE_LEDGER_QUERY_RESPONSE entries (via
// groupEntriesByTask below) or from live TASK_COST_UPDATE pushes
// whose amounts the engine read back from the ledger — so the
// popover total always reconciles with the recorded rows.

export function formatUsd(value) {
  const n = Number(value) || 0;
  if (n === 0) return '$0.00';
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

// Group ledger entries (USAGE_LEDGER_QUERY_RESPONSE shape) into the
// per-task / per-model structure the popover renders. Totals are
// summed from the entries themselves, so they cannot drift from the
// rows they summarise.
export function groupEntriesByTask(entries) {
  const byTask = new Map();
  for (const entry of entries || []) {
    const taskId = entry.task_id || 'unknown';
    let task = byTask.get(taskId);
    if (!task) {
      task = {
        task_id: taskId,
        cost_usd: 0,
        input_tokens: 0,
        output_tokens: 0,
        last_ts: 0,
        by_model: {},
      };
      byTask.set(taskId, task);
    }
    task.cost_usd += entry.cost_usd || 0;
    task.input_tokens += entry.input_tokens || 0;
    task.output_tokens += entry.output_tokens || 0;
    task.last_ts = Math.max(task.last_ts, entry.timestamp || 0);

    const modelName = entry.model_name || 'unknown';
    let model = task.by_model[modelName];
    if (!model) {
      model = {
        cost_usd: 0,
        input_tokens: 0,
        output_tokens: 0,
        calls: 0,
        is_local: false,
        unpriced: false,
      };
      task.by_model[modelName] = model;
    }
    model.cost_usd += entry.cost_usd || 0;
    model.input_tokens += entry.input_tokens || 0;
    model.output_tokens += entry.output_tokens || 0;
    model.calls += 1;
    if (entry.is_local) model.is_local = true;
    if (entry.pricing_status === 'unpriced') model.unpriced = true;
  }
  return [...byTask.values()].sort((a, b) => b.last_ts - a.last_ts);
}

const rowStyle = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: '12px',
};

export default function CostHeaderPopover({ tasks = [], totalCostUsd = 0, runningTaskIds = [] }) {
  const running = new Set(runningTaskIds || []);
  return (
    <div
      data-testid="cost-popover"
      style={{
        position: 'absolute',
        top: '100%',
        right: 0,
        marginTop: '6px',
        background: 'rgba(23, 23, 30, 0.95)',
        backdropFilter: 'blur(20px)',
        border: '1px solid rgba(255, 255, 255, 0.12)',
        borderRadius: '8px',
        padding: '10px 12px',
        minWidth: '320px',
        maxWidth: '380px',
        maxHeight: '60vh',
        overflowY: 'auto',
        zIndex: 1100,
        boxShadow: '0 8px 32px rgba(0, 0, 0, 0.4)',
        color: '#fff',
        fontSize: '12px',
      }}
    >
      <div style={{ ...rowStyle, fontWeight: 600, fontSize: '13px', marginBottom: '6px' }}>
        <span>Session cost</span>
        <span data-testid="cost-popover-total">{formatUsd(totalCostUsd)}</span>
      </div>
      {tasks.length === 0 && (
        <div style={{ color: 'rgba(255,255,255,0.55)', padding: '4px 0' }}>
          No model calls recorded yet.
        </div>
      )}
      {tasks.map((task) => (
        <div key={task.task_id} data-testid={`cost-task-${task.task_id}`} style={{ padding: '6px 0', borderTop: '1px solid rgba(255,255,255,0.07)' }}>
          <div style={rowStyle}>
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0 }}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                Task {String(task.task_id).slice(0, 8)}
              </span>
              {running.has(task.task_id) && (
                <span
                  data-testid={`cost-task-running-${task.task_id}`}
                  style={{
                    background: 'rgba(16, 185, 129, 0.2)',
                    color: '#34d399',
                    borderRadius: '999px',
                    padding: '0 6px',
                    fontSize: '10px',
                    fontWeight: 600,
                  }}
                >
                  running
                </span>
              )}
            </span>
            <span data-testid={`cost-task-total-${task.task_id}`} style={{ fontWeight: 600 }}>
              {formatUsd(task.cost_usd)}
            </span>
          </div>
          {Object.entries(task.by_model || {}).map(([modelName, model]) => (
            <div
              key={modelName}
              data-testid={`cost-model-${task.task_id}-${modelName}`}
              style={{ ...rowStyle, padding: '2px 0 0 10px', color: 'rgba(255,255,255,0.75)' }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0 }}>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{modelName}</span>
                {model.is_local && (
                  <span style={{ background: 'rgba(255,255,255,0.12)', borderRadius: '999px', padding: '0 6px', fontSize: '10px' }}>
                    local
                  </span>
                )}
                {model.unpriced && (
                  <span style={{ background: 'rgba(245, 158, 11, 0.2)', color: '#fbbf24', borderRadius: '999px', padding: '0 6px', fontSize: '10px' }}>
                    unpriced
                  </span>
                )}
              </span>
              <span style={{ display: 'flex', gap: '8px', whiteSpace: 'nowrap' }}>
                <span style={{ color: 'rgba(255,255,255,0.45)' }}>
                  {model.input_tokens} in · {model.output_tokens} out
                </span>
                <span>{formatUsd(model.cost_usd)}</span>
              </span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
