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

// ── Wave 6 (D-1): manual resume for restart-interrupted tasks ──────
// Fixtures match the REAL engine shapes: the interrupted list row is
// the audit-spine row from task_ledger.recent_tasks() (session from
// the checkpoint, sources ["audit_log"]), and the audit is
// resume_audit.build_resume_audit's dict verbatim in structure.
const INTERRUPTED_LIST_PAYLOAD = {
  tasks: [
    {
      task_id: 'task-33333333-eeee-ffff',
      session_id: 'sess-9',
      prompt: 'Index the photo library',
      summary: null,
      state: 'INTERRUPTED',
      timestamp: 1730000200,
      sources: ['audit_log'],
      cost_usd: null,
    },
  ],
  count: 1,
};

const AUDIT_READY = {
  task_id: 'task-33333333-eeee-ffff',
  session_id: 'sess-9',
  prompt: 'Index the photo library',
  state: 'INTERRUPTED',
  verdict: 'ready',
  refusal_reason: null,
  completed_steps: [
    { kind: 'delegation', label: 'Delegated to planner', detail: 'index photos', status: 'completed' },
    { kind: 'tool_call', label: 'read_file', detail: '', status: 'completed' },
  ],
  in_flight: {
    kind: 'tool_call', tool_name: 'find_files',
    tool_args: { pattern: '*.jpg' }, node: 'planner',
    classification: 'not_done',
    reason: 'find_files only reads — it has no completion record, and running it again changes nothing',
  },
  files: [{ path: '/tmp/photos/index.json', exists: true }],
  pending_approvals: [],
  coverage: { checkpoint: true, audit_transitions: 2, checkpoint_scan_capped: false, notes: [] },
};

const AUDIT_UNCERTAIN = {
  ...AUDIT_READY,
  verdict: 'uncertain',
  in_flight: {
    kind: 'tool_call', tool_name: 'write_file',
    tool_args: { path: '/tmp/photos/index.json' }, node: 'planner',
    classification: 'uncertain',
    reason: 'a file exists at /tmp/photos/index.json with no completion record — the write may already have happened',
  },
  files: [{ path: '/tmp/photos/index.json', exists: true }],
};

const AUDIT_REFUSED = {
  ...AUDIT_READY,
  verdict: 'refused',
  refusal_reason: 'the task is waiting on an approval decision — answer the approval instead of resuming',
  completed_steps: [],
  in_flight: null,
  files: [],
};

function renderInterrupted() {
  const ref = makeWsRef();
  render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
  deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', INTERRUPTED_LIST_PAYLOAD);
  fireEvent.click(screen.getByText('Index the photo library'));
  return ref;
}

describe('TaskLedgerPanel resume (D-1)', () => {
  it('renders the Interrupted by restart treatment and a Resume review entry', () => {
    const ref = renderInterrupted();
    expect(screen.getByText('Interrupted by restart')).toBeInTheDocument();
    expect(screen.getByTestId('task-resume-section')).toBeInTheDocument();
    expect(screen.getByTestId('task-resume-review-button')).toBeInTheDocument();
    // Nothing has been sent yet beyond list + detail query: resume
    // is audit-first, never one-click.
    const types = sentFrames(ref.wsRef).map((f) => f.type);
    expect(types).not.toContain('TASK_RESUME_AUDIT_REQUEST');
    expect(types).not.toContain('TASK_RESUME_REQUEST');
  });

  it('does not offer resume on a non-interrupted task', () => {
    const ref = makeWsRef();
    render(<TaskLedgerPanel wsRef={ref.wsRef} isConnected />);
    deliver(ref.handler, 'TASK_LEDGER_LIST_RESPONSE', LIST_PAYLOAD);
    fireEvent.click(screen.getByText('Summarise the repository layout'));
    expect(screen.queryByTestId('task-resume-section')).not.toBeInTheDocument();
  });

  it('audit-first flow: review fetches the audit, confirm sends the resume, success refreshes the list', () => {
    const ref = renderInterrupted();
    fireEvent.click(screen.getByTestId('task-resume-review-button'));
    const auditReq = sentFrames(ref.wsRef).find((f) => f.type === 'TASK_RESUME_AUDIT_REQUEST');
    expect(auditReq).toBeDefined();
    expect(auditReq.payload).toEqual({
      task_id: 'task-33333333-eeee-ffff', session_id: 'sess-9',
    });
    deliver(ref.handler, 'TASK_RESUME_AUDIT_RESPONSE', {
      success: true, task_id: 'task-33333333-eeee-ffff', audit: AUDIT_READY, error: null,
    });
    // Completed steps are named as done/skipped; the in-flight step
    // carries its classification in plain words.
    expect(screen.getByText('Delegated to planner')).toBeInTheDocument();
    expect(screen.getByText('find_files')).toBeInTheDocument();
    expect(screen.getByText('Not done — safe to run again')).toBeInTheDocument();
    expect(screen.getByText(/still on disk/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('task-resume-confirm-button'));
    const resumeReq = sentFrames(ref.wsRef).find((f) => f.type === 'TASK_RESUME_REQUEST');
    expect(resumeReq).toBeDefined();
    expect(resumeReq.payload).toEqual({
      task_id: 'task-33333333-eeee-ffff', session_id: 'sess-9',
    });
    expect(resumeReq.payload.confirm_uncertain).toBeUndefined();
    deliver(ref.handler, 'TASK_RESUME_RESPONSE', {
      success: true, task_id: 'task-33333333-eeee-ffff', session_id: 'sess-9',
      error: null, requires_confirmation: false, audit: AUDIT_READY,
    });
    expect(screen.getByTestId('task-resume-started')).toHaveTextContent(
      'Resume started — the task is running again.');
    const lists = sentFrames(ref.wsRef).filter((f) => f.type === 'TASK_LEDGER_LIST_REQUEST');
    expect(lists.length).toBeGreaterThanOrEqual(2);
  });

  it('uncertain audit: the confirm copy names the uncertainty and the request carries confirm_uncertain', () => {
    const ref = renderInterrupted();
    fireEvent.click(screen.getByTestId('task-resume-review-button'));
    deliver(ref.handler, 'TASK_RESUME_AUDIT_RESPONSE', {
      success: true, task_id: 'task-33333333-eeee-ffff', audit: AUDIT_UNCERTAIN, error: null,
    });
    expect(screen.getByText('Uncertain — cannot be verified')).toBeInTheDocument();
    const warning = screen.getByTestId('task-resume-uncertain-warning');
    expect(warning).toHaveTextContent('cannot be verified');
    expect(warning).toHaveTextContent('may repeat it');
    expect(screen.getByTestId('task-resume-confirm-button')).toHaveTextContent('Resume anyway');
    fireEvent.click(screen.getByTestId('task-resume-confirm-button'));
    const resumeReq = sentFrames(ref.wsRef).find((f) => f.type === 'TASK_RESUME_REQUEST');
    expect(resumeReq.payload.confirm_uncertain).toBe(true);
  });

  it('refused audit shows the refusal reason and offers no confirm', () => {
    const ref = renderInterrupted();
    fireEvent.click(screen.getByTestId('task-resume-review-button'));
    deliver(ref.handler, 'TASK_RESUME_AUDIT_RESPONSE', {
      success: true, task_id: 'task-33333333-eeee-ffff', audit: AUDIT_REFUSED, error: null,
    });
    expect(screen.getByText(/waiting on an approval decision/)).toBeInTheDocument();
    expect(screen.queryByTestId('task-resume-confirm-button')).not.toBeInTheDocument();
  });

  it('a refused resume shows the engine error verbatim', () => {
    const ref = renderInterrupted();
    fireEvent.click(screen.getByTestId('task-resume-review-button'));
    deliver(ref.handler, 'TASK_RESUME_AUDIT_RESPONSE', {
      success: true, task_id: 'task-33333333-eeee-ffff', audit: AUDIT_READY, error: null,
    });
    fireEvent.click(screen.getByTestId('task-resume-confirm-button'));
    deliver(ref.handler, 'TASK_RESUME_RESPONSE', {
      success: false, task_id: 'task-33333333-eeee-ffff', session_id: 'sess-9',
      error: 'a resume for this task is already in progress',
      requires_confirmation: false, audit: AUDIT_READY,
    });
    expect(screen.getByTestId('task-resume-error')).toHaveTextContent(
      'a resume for this task is already in progress');
  });
});
