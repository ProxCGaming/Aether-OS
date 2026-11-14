import React, { useEffect, useState } from 'react';
import { ChevronDown, ChevronRight, ListChecks, RefreshCw } from 'lucide-react';
import LoadStateRow from './Settings/LoadStateRow';

// Task Ledger (master build S3 UI): the read-only Mission Control
// surface for the engine's task ledger. The engine side
// (aether_engine/task_ledger.py) joins five existing writers —
// checkpointed graph state, the executor's live event tap, episodic
// memory, the audit trail and the usage ledger — behind two
// request/response pairs:
//
//   TASK_LEDGER_LIST_REQUEST  {}                     -> { tasks, count }
//   TASK_LEDGER_QUERY_REQUEST { task_id, session_id? } -> task summary
//
// Honesty rules, mirroring the engine's own contract:
// - Only fields the payload actually carries are rendered. A recent
//   row has no delegation count and no started/finished pair (one
//   `timestamp`), so the list shows neither; the detail renders
//   delegation_count only when the summary carries one.
// - An unknown / aged-out task answers found: False. That renders
//   as "no longer in the ledger's records" plus the summary's own
//   coverage notes verbatim — never as an error and never dressed
//   up as a fabricated record.
// - Loading / empty / error / offline are distinct states (the
//   LoadStateRow idiom): an unanswered request is never shown as
//   an empty ledger.

const SOURCE_LABELS = {
  episodic_memory: 'episodic memory',
  executor_event: 'live tap',
  checkpoint_state: 'checkpoint',
  audit_log: 'audit trail',
};

// The delegation's model_source is the routing decision's source
// (S14): the AGENT_ROUTING labels where they overlap, plus the
// escalation case the tap records. Unknown values render raw.
const MODEL_SOURCE_LABELS = {
  auto: 'Auto routing',
  escalation: 'Escalated',
  pinned: 'Pinned by you',
  run_default: 'Run default',
};

function fmtTime(epochSeconds) {
  if (typeof epochSeconds !== 'number' || !Number.isFinite(epochSeconds)) return null;
  return new Date(epochSeconds * 1000).toLocaleString();
}

function fmtCost(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return `$${value.toFixed(4)}`;
}

function shortId(taskId) {
  const id = String(taskId || '');
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

function chipStyle(accent) {
  return {
    display: 'inline-block',
    fontSize: '11px',
    lineHeight: 1.5,
    padding: '1px 8px',
    borderRadius: '999px',
    border: `1px solid ${accent ? 'var(--accent)' : 'var(--border-default)'}`,
    color: accent ? 'var(--accent)' : 'var(--text-muted)',
    whiteSpace: 'nowrap',
  };
}

const sectionLabelStyle = {
  fontSize: '12px',
  textTransform: 'uppercase',
  letterSpacing: '1px',
  color: 'var(--text-muted)',
  margin: '18px 0 8px 0',
};

function DelegationRow({ delegation }) {
  const modelBits = [];
  if (delegation.model) {
    modelBits.push(delegation.model_provider
      ? `${delegation.model} (${delegation.model_provider})`
      : delegation.model);
  }
  return (
    <div style={{ padding: '8px 0', borderTop: '1px solid var(--border-default)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        <span style={{ fontWeight: 500 }}>{delegation.agent || 'unknown agent'}</span>
        {delegation.denied && <span style={chipStyle(false)}>denied</span>}
        {delegation.model_source && (
          <span style={chipStyle(true)}>
            {MODEL_SOURCE_LABELS[delegation.model_source] || delegation.model_source}
          </span>
        )}
        {fmtTime(delegation.timestamp) && (
          <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{fmtTime(delegation.timestamp)}</span>
        )}
      </div>
      {modelBits.length > 0 && (
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '2px' }}>
          Model: {modelBits.join(' ')}
        </div>
      )}
      {delegation.reason && (
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '2px' }}>
          {delegation.reason}
        </div>
      )}
    </div>
  );
}

function TaskDetail({ detail }) {
  if (detail.state === 'loading') {
    return (
      <div style={{ fontSize: '13px', color: 'var(--text-muted)', padding: '10px 0' }}>
        Loading task detail…
      </div>
    );
  }
  if (detail.state === 'error') {
    return (
      <div style={{ fontSize: '13px', color: 'var(--danger)', padding: '10px 0' }}>
        {detail.error}
      </div>
    );
  }
  const summary = detail.summary || {};
  const coverage = summary.coverage || {};
  const coverageNotes = coverage.notes || [];
  if (!summary.found) {
    // The engine's honest not-found: the task aged out of every
    // writer the ledger joins (or ran before they existed). The
    // coverage notes say which, in the engine's own words.
    return (
      <div style={{ padding: '10px 0', fontSize: '14px' }}>
        <div style={{ color: 'var(--text-secondary)' }}>
          This task is no longer in the ledger&rsquo;s records.
        </div>
        {coverageNotes.map((note, i) => (
          <div key={i} style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '4px' }}>{note}</div>
        ))}
      </div>
    );
  }

  const outcome = summary.outcome || {};
  const delegations = summary.delegations || [];
  const subagents = summary.subagents || [];
  const models = summary.models || [];
  const costs = summary.costs || {};
  const errors = summary.errors || [];
  const files = summary.files_touched || [];

  return (
    <div style={{ padding: '4px 0 8px 0', fontSize: '14px' }}>
      <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', color: 'var(--text-secondary)', fontSize: '13px' }}>
        <span>Task {summary.task_id}</span>
        {summary.phase && <span>Phase: {summary.phase}</span>}
        {typeof summary.latency_ms === 'number' && (
          <span>Latency: {Math.round(summary.latency_ms)} ms</span>
        )}
        {summary.delegation_count !== null && summary.delegation_count !== undefined && (
          <span>Delegations: {summary.delegation_count}</span>
        )}
      </div>

      {summary.prompt && (
        <div style={{ marginTop: '10px', color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>
          {summary.prompt}
        </div>
      )}

      {(outcome.summary || outcome.response_excerpt) && (
        <>
          <div style={sectionLabelStyle}>Outcome</div>
          {outcome.summary && <div>{outcome.summary}</div>}
          {outcome.response_excerpt && (
            <div style={{ color: 'var(--text-secondary)', marginTop: outcome.summary ? '6px' : 0, whiteSpace: 'pre-wrap' }}>
              {outcome.response_excerpt}
              {outcome.response_truncated ? '…' : ''}
            </div>
          )}
          {Array.isArray(outcome.tags) && outcome.tags.length > 0 && (
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '8px' }}>
              {outcome.tags.map((tag, i) => <span key={i} style={chipStyle(false)}>{String(tag)}</span>)}
            </div>
          )}
        </>
      )}

      {delegations.length > 0 && (
        <>
          <div style={sectionLabelStyle}>Delegations</div>
          {delegations.map((d, i) => <DelegationRow key={i} delegation={d} />)}
        </>
      )}

      {subagents.length > 0 && (
        <>
          <div style={sectionLabelStyle}>Sub-agents</div>
          {subagents.map((s, i) => (
            <div key={i} style={{ padding: '6px 0', borderTop: '1px solid var(--border-default)' }}>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ fontWeight: 500 }}>{s.role || 'sub-agent'}</span>
                {s.seq !== null && s.seq !== undefined && <span style={chipStyle(false)}>#{s.seq}</span>}
                {s.status && <span style={chipStyle(false)}>{s.status}</span>}
                {s.parent_agent && (
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>by {s.parent_agent}</span>
                )}
                {typeof s.tokens_used === 'number' && (
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{s.tokens_used} tokens</span>
                )}
              </div>
              {s.summary && (
                <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '2px' }}>{s.summary}</div>
              )}
            </div>
          ))}
        </>
      )}

      {(models.length > 0 || typeof costs.total_cost_usd === 'number') && (
        <>
          <div style={sectionLabelStyle}>Usage</div>
          {models.map((m, i) => (
            <div key={i} style={{ display: 'flex', gap: '12px', flexWrap: 'wrap', padding: '4px 0', fontSize: '13px', color: 'var(--text-secondary)' }}>
              <span>{m.agent || 'unknown agent'}</span>
              <span>{m.provider ? `${m.model} (${m.provider})` : m.model}</span>
              <span>×{m.calls}</span>
              <span>{(m.input_tokens || 0) + (m.output_tokens || 0)} tokens</span>
              {fmtCost(m.cost_usd) && <span>{fmtCost(m.cost_usd)}</span>}
            </div>
          ))}
          {typeof costs.total_cost_usd === 'number' && (
            <div style={{ fontSize: '13px', marginTop: '4px' }}>
              Total: {fmtCost(costs.total_cost_usd)}
              {typeof costs.input_tokens === 'number' && typeof costs.output_tokens === 'number' && (
                <span style={{ color: 'var(--text-muted)' }}>
                  {' '}· {costs.input_tokens} in / {costs.output_tokens} out tokens
                </span>
              )}
            </div>
          )}
        </>
      )}

      {files.length > 0 && (
        <>
          <div style={sectionLabelStyle}>Files touched</div>
          {files.map((f, i) => (
            <div key={i} style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>{f}</div>
          ))}
        </>
      )}

      {errors.length > 0 && (
        <>
          <div style={sectionLabelStyle}>Errors</div>
          {errors.map((e, i) => (
            <div key={i} style={{ fontSize: '13px', color: 'var(--danger)' }}>{e.message}</div>
          ))}
        </>
      )}

      <div style={sectionLabelStyle}>Coverage</div>
      <div style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
        <div>
          Sources: checkpoint {coverage.checkpoint ? 'yes' : 'no'}
          {' · '}episode {coverage.episode ? 'yes' : 'no'}
          {' · '}live tap {coverage.live_tap ? 'yes' : 'no'}
          {' · '}audit records {coverage.audit_records ?? 0}
          {' · '}usage calls {coverage.usage_calls ?? 0}
        </div>
        {coverageNotes.map((note, i) => (
          <div key={i} style={{ color: 'var(--text-muted)' }}>{note}</div>
        ))}
      </div>
    </div>
  );
}

export default function TaskLedgerPanel({ wsRef = null, reconnectCount = 0, isConnected = null }) {
  const [loadState, setLoadState] = useState('loading');
  const [listError, setListError] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [expandedId, setExpandedId] = useState(null);
  const [details, setDetails] = useState({});

  // Dashboard's socket pattern: own message listener on the shared
  // wsRef, requests sent as raw JSON frames with the shared
  // envelope. reconnectCount re-runs the effect so a list response
  // lost across a disconnect is re-fetched (D2-09); refreshNonce
  // does the same for the manual Refresh button.
  useEffect(() => {
    if (!wsRef?.current) return undefined;
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'TASK_LEDGER_LIST_RESPONSE') {
          if (data.payload && data.payload.error) {
            setListError(data.payload.error);
            setLoadState('error');
          } else {
            setListError(null);
            setTasks(Array.isArray(data.payload?.tasks) ? data.payload.tasks : []);
            setLoadState('ready');
          }
        } else if (data.type === 'TASK_LEDGER_QUERY_RESPONSE') {
          const payload = data.payload || {};
          const taskId = payload.task_id;
          if (!taskId) return;
          if (payload.error) {
            setDetails((prev) => ({
              ...prev,
              [taskId]: { state: 'error', error: payload.error },
            }));
          } else {
            setDetails((prev) => ({
              ...prev,
              [taskId]: { state: 'ready', summary: payload },
            }));
          }
        }
      } catch (e) {
        // ignore malformed frames
      }
    };
    wsRef.current.addEventListener('message', handleMessage);
    wsRef.current.send(JSON.stringify({
      type: 'TASK_LEDGER_LIST_REQUEST', schema_version: 1,
      request_id: Date.now().toString(), payload: {},
    }));
    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
  }, [wsRef, reconnectCount, refreshNonce]);

  const toggleTask = (task) => {
    const taskId = task.task_id;
    if (expandedId === taskId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(taskId);
    if (!details[taskId] && wsRef?.current) {
      setDetails((prev) => ({ ...prev, [taskId]: { state: 'loading' } }));
      const payload = { task_id: taskId };
      if (task.session_id) payload.session_id = task.session_id;
      wsRef.current.send(JSON.stringify({
        type: 'TASK_LEDGER_QUERY_REQUEST', schema_version: 1,
        request_id: Date.now().toString(), payload,
      }));
    }
  };

  const handleRefresh = () => {
    setDetails({});
    setExpandedId(null);
    setLoadState('loading');
    setRefreshNonce((n) => n + 1);
  };

  return (
    <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)' }}>
          <ListChecks size={18} />
          <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Task Ledger</span>
        </div>
        <button
          onClick={handleRefresh}
          style={{ background: 'transparent', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '6px 12px', borderRadius: 'var(--radius-lg)', display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer', fontSize: '13px' }}
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      <LoadStateRow
        loadState={loadState}
        isConnected={isConnected}
        loadingText="Loading the task ledger…"
        offlineText="Waiting for the engine — the task ledger will load once it connects."
        errorText={listError ? `The task ledger could not be loaded. ${listError}` : 'The task ledger could not be loaded.'}
        testId="task-ledger-load-state"
      />

      {loadState === 'ready' && tasks.length === 0 && (
        <div style={{ fontSize: '14px', color: 'var(--text-muted)' }}>
          No tasks recorded yet — tasks appear here after the supervisor runs.
        </div>
      )}

      {loadState === 'ready' && tasks.map((task) => {
        const expanded = expandedId === task.task_id;
        const title = task.prompt || task.summary || task.task_id;
        return (
          <div key={task.task_id} style={{ borderTop: '1px solid var(--border-default)', padding: '10px 0' }}>
            <button
              onClick={() => toggleTask(task)}
              aria-expanded={expanded}
              style={{ background: 'transparent', border: 'none', color: 'inherit', cursor: 'pointer', display: 'flex', alignItems: 'flex-start', gap: '8px', width: '100%', textAlign: 'left', padding: 0 }}
            >
              {expanded ? <ChevronDown size={16} style={{ marginTop: '2px', flexShrink: 0 }} /> : <ChevronRight size={16} style={{ marginTop: '2px', flexShrink: 0 }} />}
              <span style={{ flex: 1, minWidth: 0 }}>
                <span style={{ display: 'block', fontSize: '14px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {title}
                </span>
                <span style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', fontSize: '12px', color: 'var(--text-muted)', marginTop: '3px' }}>
                  <span>{shortId(task.task_id)}</span>
                  {task.state && <span>{task.state}</span>}
                  {fmtTime(task.timestamp) && <span>{fmtTime(task.timestamp)}</span>}
                  {fmtCost(task.cost_usd) && <span>{fmtCost(task.cost_usd)}</span>}
                  {Array.isArray(task.sources) && task.sources.length > 0 && (
                    <span>{task.sources.map((s) => SOURCE_LABELS[s] || s).join(' · ')}</span>
                  )}
                </span>
              </span>
            </button>
            {expanded && details[task.task_id] && <TaskDetail detail={details[task.task_id]} />}
            {expanded && !details[task.task_id] && (
              <div style={{ fontSize: '13px', color: 'var(--text-muted)', padding: '10px 0' }}>
                Loading task detail…
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
