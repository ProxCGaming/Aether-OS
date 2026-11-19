/**
 * Workflows manager: the saved step sequences behind the
 * Dashboard's workflow picker. The list, the editor and the run
 * strip all ride the WORKFLOW_* pairs; rows change only on the
 * engine's confirming reply, a refusal shows the engine's reason
 * verbatim and keeps the editor open, and a failed read is an
 * error state — never an empty list dressed as an answer.
 */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import WorkflowsPanel from '../WorkflowsPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeHarness({ isConnected = true } = {}) {
  let messageListeners = [];
  const mockWs = {
    addEventListener: jest.fn((event, cb) => {
      if (event === 'message') messageListeners.push(cb);
    }),
    removeEventListener: jest.fn((event, cb) => {
      if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
    }),
  };
  const sendMessage = jest.fn();
  useAetherSocket.mockReturnValue({
    wsRef: { current: mockWs },
    sendMessage,
    isConnected,
  });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  const callsOf = (type) => sendMessage.mock.calls.filter(([t]) => t === type);
  return { simulateMessage, sendMessage, callsOf };
}

const WORKFLOWS = [
  {
    id: 'w1',
    name: 'Morning research',
    description: 'Headlines, then a summary',
    steps: [
      { title: 'Gather headlines', prompt: 'Find today\'s top tech headlines' },
      { title: 'Summarise', prompt: 'Summarise the headlines in five bullets' },
    ],
    created_at: 1760000000,
    updated_at: 1760000100,
  },
  {
    id: 'w2',
    name: 'Inbox sweep',
    description: '',
    steps: [{ title: '', prompt: 'Triage my inbox' }],
    created_at: 1760000200,
    updated_at: 1760000200,
  },
];

function renderWithList(harness, workflows = WORKFLOWS) {
  render(<WorkflowsPanel />);
  harness.simulateMessage({ type: 'WORKFLOW_LIST_RESPONSE', payload: { workflows } });
}

describe('WorkflowsPanel list', () => {
  beforeEach(() => {
    window.confirm = jest.fn(() => true);
  });

  test('requests the workflows and the runs on mount and shows a load state until the list answers', () => {
    const { sendMessage } = makeHarness();
    render(<WorkflowsPanel />);
    expect(sendMessage).toHaveBeenCalledWith(
      'WORKFLOW_LIST_REQUEST',
      expect.objectContaining({ payload: {} }),
    );
    expect(sendMessage).toHaveBeenCalledWith(
      'WORKFLOW_RUN_LIST_REQUEST',
      expect.objectContaining({ payload: {} }),
    );
    expect(screen.getByTestId('workflows-load-state')).toBeInTheDocument();
    expect(screen.queryByText('No workflows yet')).not.toBeInTheDocument();
  });

  test('renders each workflow with its name, step count and description', () => {
    const harness = makeHarness();
    renderWithList(harness);
    expect(screen.getByText('Morning research')).toBeInTheDocument();
    expect(screen.getByText('2 steps')).toBeInTheDocument();
    expect(screen.getByText('Headlines, then a summary')).toBeInTheDocument();
    expect(screen.getByText('Inbox sweep')).toBeInTheDocument();
    expect(screen.getByText('1 step')).toBeInTheDocument();
  });

  test('an answered-empty list shows the shared empty state with the create action', () => {
    const harness = makeHarness();
    renderWithList(harness, []);
    expect(screen.getByTestId('workflows-empty')).toBeInTheDocument();
    expect(screen.getByText('No workflows yet')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Create your first workflow'));
    expect(screen.getByTestId('workflow-editor')).toBeInTheDocument();
  });

  test('a failed list read is an error state, never an empty list', () => {
    const harness = makeHarness();
    render(<WorkflowsPanel />);
    harness.simulateMessage({
      type: 'WORKFLOW_LIST_RESPONSE',
      payload: { success: false, error: 'The workflow store is unavailable.' },
    });
    expect(screen.getByTestId('workflows-load-state')).toBeInTheDocument();
    expect(screen.getByText('The workflow store is unavailable.')).toBeInTheDocument();
    expect(screen.queryByText('No workflows yet')).not.toBeInTheDocument();
  });
});

describe('WorkflowsPanel editor', () => {
  beforeEach(() => {
    window.confirm = jest.fn(() => true);
  });

  test('a new workflow starts with one blank step; steps can be added, removed and reordered', () => {
    const harness = makeHarness();
    renderWithList(harness);
    fireEvent.click(screen.getByText('New workflow'));
    expect(screen.getByTestId('workflow-editor')).toBeInTheDocument();
    expect(screen.getByLabelText('Step 1 prompt')).toBeInTheDocument();
    expect(screen.queryByLabelText('Step 2 prompt')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Add step'));
    expect(screen.getByLabelText('Step 2 prompt')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Step 1 prompt'), { target: { value: 'First task' } });
    fireEvent.change(screen.getByLabelText('Step 2 prompt'), { target: { value: 'Second task' } });
    fireEvent.click(screen.getByLabelText('Move step 2 up'));
    expect(screen.getByLabelText('Step 1 prompt')).toHaveValue('Second task');
    expect(screen.getByLabelText('Step 2 prompt')).toHaveValue('First task');
    fireEvent.click(screen.getByLabelText('Move step 1 down'));
    expect(screen.getByLabelText('Step 1 prompt')).toHaveValue('First task');

    fireEvent.click(screen.getByLabelText('Remove step 2'));
    expect(screen.queryByLabelText('Step 2 prompt')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Step 1 prompt')).toHaveValue('First task');
  });

  test('save is refused with a visible reason while the name or a step prompt is blank', () => {
    const harness = makeHarness();
    renderWithList(harness);
    fireEvent.click(screen.getByText('New workflow'));
    const save = screen.getByText('Save workflow');
    expect(save).toBeDisabled();
    expect(screen.getByText('Give the workflow a name to save it.')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Daily brief' } });
    expect(save).toBeDisabled();
    expect(screen.getByText('Every step needs a prompt — fill in the blank step or remove it.')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Step 1 prompt'), { target: { value: 'Write the brief' } });
    expect(save).not.toBeDisabled();
    expect(harness.callsOf('WORKFLOW_SAVE_REQUEST')).toHaveLength(0);
  });

  test('creating sends a save without an id; the editor closes and the list refreshes only on the confirming reply', () => {
    const harness = makeHarness();
    renderWithList(harness);
    fireEvent.click(screen.getByText('New workflow'));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Daily brief' } });
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'The morning brief' } });
    fireEvent.change(screen.getByLabelText('Step 1 title'), { target: { value: 'Draft' } });
    fireEvent.change(screen.getByLabelText('Step 1 prompt'), { target: { value: 'Write the brief' } });
    fireEvent.click(screen.getByText('Save workflow'));

    const saves = harness.callsOf('WORKFLOW_SAVE_REQUEST');
    expect(saves).toHaveLength(1);
    expect(saves[0][1].payload).toEqual({
      workflow: {
        name: 'Daily brief',
        description: 'The morning brief',
        steps: [{ title: 'Draft', prompt: 'Write the brief' }],
      },
    });
    // No confirming reply yet: the editor is still open and the
    // list behind it is unchanged.
    expect(screen.getByTestId('workflow-editor')).toBeInTheDocument();

    harness.simulateMessage({ type: 'WORKFLOW_SAVE_RESPONSE', payload: { success: true, workflow: { id: 'w3' } } });
    expect(screen.queryByTestId('workflow-editor')).not.toBeInTheDocument();
    expect(harness.callsOf('WORKFLOW_LIST_REQUEST')).toHaveLength(2);
  });

  test('editing sends the workflow id with the updated steps', () => {
    const harness = makeHarness();
    renderWithList(harness);
    fireEvent.click(screen.getAllByText('Edit')[0]);
    expect(screen.getByText('Edit workflow')).toBeInTheDocument();
    expect(screen.getByLabelText('Name')).toHaveValue('Morning research');
    expect(screen.getByLabelText('Step 2 prompt')).toHaveValue('Summarise the headlines in five bullets');
    fireEvent.change(screen.getByLabelText('Step 1 prompt'), { target: { value: 'Find today\'s top science headlines' } });
    fireEvent.click(screen.getByText('Save workflow'));

    const saves = harness.callsOf('WORKFLOW_SAVE_REQUEST');
    expect(saves).toHaveLength(1);
    expect(saves[0][1].payload.workflow.id).toBe('w1');
    expect(saves[0][1].payload.workflow.steps).toEqual([
      { title: 'Gather headlines', prompt: 'Find today\'s top science headlines' },
      { title: 'Summarise', prompt: 'Summarise the headlines in five bullets' },
    ]);
  });

  test('a save refusal shows the engine reason verbatim and keeps the editor open', () => {
    const harness = makeHarness();
    renderWithList(harness);
    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.click(screen.getByText('Save workflow'));
    harness.simulateMessage({
      type: 'WORKFLOW_SAVE_RESPONSE',
      payload: { success: false, error: 'A workflow step prompt must not be empty.' },
    });
    expect(screen.getByText('A workflow step prompt must not be empty.')).toBeInTheDocument();
    expect(screen.getByTestId('workflow-editor')).toBeInTheDocument();
    expect(harness.callsOf('WORKFLOW_LIST_REQUEST')).toHaveLength(1);
  });
});

describe('WorkflowsPanel delete', () => {
  test('delete asks first; cancelling the confirm sends nothing', () => {
    window.confirm = jest.fn(() => false);
    const harness = makeHarness();
    renderWithList(harness);
    fireEvent.click(screen.getAllByText('Delete')[0]);
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('Morning research'));
    expect(harness.callsOf('WORKFLOW_DELETE_REQUEST')).toHaveLength(0);
  });

  test('a confirmed delete sends the workflow id and refreshes on the confirming reply', () => {
    window.confirm = jest.fn(() => true);
    const harness = makeHarness();
    renderWithList(harness);
    fireEvent.click(screen.getAllByText('Delete')[1]);
    const deletes = harness.callsOf('WORKFLOW_DELETE_REQUEST');
    expect(deletes).toHaveLength(1);
    expect(deletes[0][1].payload).toEqual({ workflow_id: 'w2' });
    harness.simulateMessage({ type: 'WORKFLOW_DELETE_RESPONSE', payload: { success: true } });
    expect(harness.callsOf('WORKFLOW_LIST_REQUEST')).toHaveLength(2);
  });

  test('a delete refusal surfaces the engine reason', () => {
    window.confirm = jest.fn(() => true);
    const harness = makeHarness();
    renderWithList(harness);
    fireEvent.click(screen.getAllByText('Delete')[0]);
    harness.simulateMessage({
      type: 'WORKFLOW_DELETE_RESPONSE',
      payload: { success: false, error: 'The workflow store is read-only right now.' },
    });
    expect(screen.getByText('The workflow store is read-only right now.')).toBeInTheDocument();
  });
});

describe('WorkflowsPanel run strip', () => {
  beforeEach(() => {
    window.confirm = jest.fn(() => true);
  });

  const RUNNING_RUN = {
    run_id: 'r1',
    workflow_id: 'w1',
    session_id: 's1',
    status: 'running',
    current_step: 0,
    steps: [
      { title: 'Gather headlines', status: 'running', task_id: 't1' },
      { title: 'Summarise', status: 'pending', task_id: null },
    ],
  };

  test('no runs, no strip', () => {
    const harness = makeHarness();
    renderWithList(harness);
    harness.simulateMessage({ type: 'WORKFLOW_RUN_LIST_RESPONSE', payload: { runs: [] } });
    expect(screen.queryByTestId('workflow-runs')).not.toBeInTheDocument();
  });

  test('an active run shows its workflow, status and step; Stop sends the stop request', () => {
    const harness = makeHarness();
    renderWithList(harness);
    harness.simulateMessage({ type: 'WORKFLOW_RUN_LIST_RESPONSE', payload: { runs: [RUNNING_RUN] } });
    expect(screen.getByTestId('workflow-runs')).toBeInTheDocument();
    expect(screen.getByText('Running · Step 1 of 2 — Gather headlines')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Stop'));
    const stops = harness.callsOf('WORKFLOW_RUN_STOP_REQUEST');
    expect(stops).toHaveLength(1);
    expect(stops[0][1].payload).toEqual({ run_id: 'r1' });
  });

  test('status events upsert the run: an approval pause reads plainly and keeps the Stop button', () => {
    const harness = makeHarness();
    renderWithList(harness);
    harness.simulateMessage({ type: 'WORKFLOW_RUN_LIST_RESPONSE', payload: { runs: [RUNNING_RUN] } });
    harness.simulateMessage({
      type: 'WORKFLOW_RUN_STATUS',
      payload: {
        ...RUNNING_RUN,
        status: 'awaiting_approval',
        current_step: 1,
        steps: [
          { title: 'Gather headlines', status: 'completed', task_id: 't1' },
          { title: 'Summarise', status: 'awaiting_approval', task_id: 't2' },
        ],
      },
    });
    expect(screen.getByText('Waiting for your approval · Step 2 of 2 — Summarise')).toBeInTheDocument();
    expect(screen.getByText('Stop')).toBeInTheDocument();
  });

  test('a finished run shows its final status and no Stop button', () => {
    const harness = makeHarness();
    renderWithList(harness);
    harness.simulateMessage({
      type: 'WORKFLOW_RUN_LIST_RESPONSE',
      payload: {
        runs: [
          { ...RUNNING_RUN, run_id: 'r2', status: 'interrupted', current_step: 1 },
          {
            ...RUNNING_RUN,
            status: 'completed',
            current_step: 1,
            steps: [
              { title: 'Gather headlines', status: 'completed', task_id: 't1' },
              { title: 'Summarise', status: 'completed', task_id: 't2' },
            ],
          },
        ],
      },
    });
    expect(screen.getByText(/Interrupted by restart/)).toBeInTheDocument();
    expect(screen.getByText(/Completed/)).toBeInTheDocument();
    expect(screen.queryByText('Stop')).not.toBeInTheDocument();
  });

  test('a stop refusal surfaces the engine reason in the strip', () => {
    const harness = makeHarness();
    renderWithList(harness);
    harness.simulateMessage({ type: 'WORKFLOW_RUN_LIST_RESPONSE', payload: { runs: [RUNNING_RUN] } });
    fireEvent.click(screen.getByText('Stop'));
    harness.simulateMessage({
      type: 'WORKFLOW_RUN_STOP_RESPONSE',
      payload: { success: false, error: 'That run has already finished.' },
    });
    expect(screen.getByText('That run has already finished.')).toBeInTheDocument();
  });
});
