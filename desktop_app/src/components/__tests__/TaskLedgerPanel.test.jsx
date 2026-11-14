import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import TaskLedgerPanel from '../TaskLedgerPanel';

// Task Ledger panel (master build S3 UI). Fixtures match the REAL
// engine shapes in aether_engine/task_ledger.py:
// - recent_tasks() rows: { task_id, session_id, prompt, summary,
//   state, timestamp (epoch seconds), sources, cost_usd } — no
//   delegation count, no started/finished pair.
// - task_summary(): { task_id, found, session_id, prompt, state,
//   phase, latency_ms, outcome{...}, delegations[...], subagents[...],
//   delegation_count, models[...], costs{...}, errors[...],
//   files_touched[...], coverage{..., notes[...]} }.
function makeWsRef() {
  const ref = { handler: null };
  ref.wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((ev, cb) => { ref.handler = cb; }),
      removeEventListener: jest.fn(),
    },
  };
  return ref;
}

const sentFrames = (wsRef) =>
  wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));

const deliver = (handler, type, payload) => {
  // eslint-disable-next-line testing-library/no-unnecessary-act
  const { act } = require('@testing-library/react');
  act(() => {
    handler({ data: JSON.stringify({ type, payload }) });
  });
};

const LIST_PAYLOAD = {
  tasks: [
    {
      task_id: 'task-11111111-aaaa-bbbb',
      session_id: 'sess-1',
      prompt: 'Summarise the repository layout',
      summary: 'Done',
      state: 'SUCCEEDED',
      timestamp: 1730000000,
      sources: ['episodic_memory'],
      cost_usd: 0.0123,
    },
    {
      task_id: 'task-22222222-cccc-dddd',
      session_id: null,
      prompt: null,
      summary: null,
      state: null,
      timestamp: 1730000100,
      sources: ['executor_event'],
      cost_usd: null,
    },
  ],
  count: 2,
};

const SUMMARY_PAYLOAD = {
  task_id: 'task-11111111-aaaa-bbbb',
  found: true,
  session_id: 'sess-1',
  prompt: 'Summarise the repository layout',
  state: 'SUCCEEDED',
  phase: 'complete',
  latency_ms: 4200,
  outcome: {
    status: 'SUCCEEDED',
    summary: 'Produced a layout summary.',
    response_excerpt: 'The repo has two halves…',
    response_truncated: false,
    tags: ['research'],
    recorded_at: 1730000000,
  },
  delegations: [
    {
      agent: 'researcher',
      reason: 'Needs a repo-wide scan',
      briefing: '',
      denied: false,
      timestamp: 1729999900,
      source: 'executor_event',
      model: 'gemini-3.5-flash',
      model_provider: 'google_gemini',
      model_source: 'auto',
    },
    {
      agent: 'coder',
      reason: '',
      briefing: '',
      denied: true,
      timestamp: 1729999950,
      source: 'checkpoint_state',
    },
  ],
  subagents: [
    {
      parent_agent: 'researcher',
      role: 'planner',
      action: 'subagent_completed',
      status: 'completed',
      seq: 1,
      summary: 'Scanned the tree.',
      tokens_used: 512,
      timestamp: 1729999960,
      source: 'executor_event',
    },
  ],
  delegation_count: 2,
  models: [
    {
      provider: 'google_gemini', model: 'gemini-3.5-flash', agent: 'researcher',
      calls: 3, cost_usd: 0.0123, input_tokens: 100, output_tokens: 50,
    },
  ],
  costs: { total_cost_usd: 0.0123, input_tokens: 100, output_tokens: 50, by_model: {}, by_agent: {} },
  errors: [],
  files_touched: [],
  coverage: {
    checkpoint: true, episode: true, audit_records: 2, usage_calls: 3,
    live_tap: true, notes: [],
  },
};

describe('TaskLedgerPanel', () => {
  it('sends TASK_LEDGER_LIST_REQUEST on mount with the shared envelope', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    const frames = sentFrames(ref.wsRef);
    const list = frames.find((f) => f.type === 'TASK_LEDGER_LIST_REQUEST');
    expect(list).toBeDefined();
    expect(list.schema_version).toBe(1);
    expect(list.payload).toEqual({});
    expect(screen.getByText(/Loading the task ledger/)).toBeInTheDocument();
  });

  it('renders recent-task rows from the real list shape', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', LIST_PAYLOAD);
    expect(screen.getByText('Summarise the repository layout')).toBeInTheDocument();
    expect(screen.getByText('SUCCEEDED')).toBeInTheDocument();
    expect(screen.getByText('$0.0123')).toBeInTheDocument();
    expect(screen.getByText(/episodic memory/)).toBeInTheDocument();
    // A row with no prompt/summary falls back to its task id, and a
    // null state/cost renders nothing invented in their place.
    expect(screen.getByText('task-22222222-cccc-dddd')).toBeInTheDocument();
    expect(screen.getByText(/live tap/)).toBeInTheDocument();
  });

  it('renders the honest empty state when the engine reports no tasks', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', { tasks: [], count: 0 });
    expect(screen.getByText(
      'No tasks recorded yet — tasks appear here after the supervisor runs.')).toBeInTheDocument();
  });

  it('shows the offline note instead of an empty ledger while the socket is down', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected={false} />);
    expect(screen.getByText(
      'Waiting for the engine — the task ledger will load once it connects.')).toBeInTheDocument();
    expect(screen.queryByText(/No tasks recorded yet/)).not.toBeInTheDocument();
  });

  it('expanding a row sends the query for that task and renders the detail', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', LIST_PAYLOAD);
    fireEvent.click(screen.getByText('Summarise the repository layout'));
    const query = sentFrames(ref.wsRef).find((f) => f.type === 'TASK_LEDGER_QUERY_REQUEST');
    expect(query).toBeDefined();
    expect(query.payload).toEqual({
      task_id: 'task-11111111-aaaa-bbbb', session_id: 'sess-1',
    });
    deliver(ref.handler, 'TASK_LEDGER_QUERY_RESPONSE', SUMMARY_PAYLOAD);
    // Outcome + delegation detail, incl. the serving model and its
    // routing source chip (S14 annotation on the delegation).
    expect(screen.getByText('Produced a layout summary.')).toBeInTheDocument();
    expect(screen.getByText('Needs a repo-wide scan')).toBeInTheDocument();
    expect(screen.getByText(/Model: gemini-3\.5-flash \(google_gemini\)/)).toBeInTheDocument();
    expect(screen.getByText('Auto routing')).toBeInTheDocument();
    expect(screen.getByText('denied')).toBeInTheDocument();
    // Sub-agent run and usage rollup.
    expect(screen.getByText('planner')).toBeInTheDocument();
    expect(screen.getByText('Scanned the tree.')).toBeInTheDocument();
    expect(screen.getByText(/Total: \$0\.0123/)).toBeInTheDocument();
    // The coverage block names its sources.
    expect(screen.getByText(/checkpoint yes/)).toBeInTheDocument();
  });

  it('renders found: False as the honest no-longer-recorded note, with the coverage notes', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', LIST_PAYLOAD);
    fireEvent.click(screen.getByText('task-22222222-cccc-dddd'));
    deliver(ref.handler, 'TASK_LEDGER_QUERY_RESPONSE', {
      task_id: 'task-22222222-cccc-dddd',
      found: false,
      coverage: {
        checkpoint: false, episode: false, audit_records: 0, usage_calls: 0,
        live_tap: false,
        notes: ['No live-tap records: the task ran before this build\'s tap existed or in a previous engine process (the tap is in-memory and bounded).'],
      },
    });
    expect(screen.getByText(
      /This task is no longer in the ledger.s records\./)).toBeInTheDocument();
    expect(screen.getByText(/No live-tap records/)).toBeInTheDocument();
  });

  it('renders a query error reply verbatim instead of a detail', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', LIST_PAYLOAD);
    fireEvent.click(screen.getByText('Summarise the repository layout'));
    deliver(ref.handler, 'TASK_LEDGER_QUERY_RESPONSE', {
      task_id: 'task-11111111-aaaa-bbbb', found: false, error: 'task_id is required',
    });
    expect(screen.getByText('task_id is required')).toBeInTheDocument();
    expect(screen.queryByText(/no longer in the ledger/)).not.toBeInTheDocument();
  });

  it('renders a list error reply verbatim', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', { error: 'ledger unavailable' });
    expect(screen.getByText(/ledger unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/No tasks recorded yet/)).not.toBeInTheDocument();
  });

  it('Refresh re-sends the list request', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', LIST_PAYLOAD);
    fireEvent.click(screen.getByText('Refresh'));
    const lists = sentFrames(ref.wsRef).filter((f) => f.type === 'TASK_LEDGER_LIST_REQUEST');
    expect(lists).toHaveLength(2);
  });

  it('re-fetches the list when reconnectCount changes', () => {
    const ref = makeWsRef();
    const { rerender } = render(
      <TaskLedgerPanel wsRef={ref.wsRef} reconnectCount={0} isConnected />);
    rerender(<TaskLedgerPanel wsRef={ref.wsRef} reconnectCount={1} isConnected />);
    const lists = sentFrames(ref.wsRef).filter((f) => f.type === 'TASK_LEDGER_LIST_REQUEST');
    expect(lists).toHaveLength(2);
  });
});
