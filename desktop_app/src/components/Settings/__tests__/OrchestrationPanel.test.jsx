import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import OrchestrationPanel from '../OrchestrationPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

// OrchestrationPanel (E6-F05) is controlled: SettingsPanel owns the
// socket and the applied config; the panel renders the applied
// config and reports edits as partial-config payloads through
// onConfigUpdate. These tests pin the per-panel contract the commit
// gate requires (a <Name>.test.jsx per component); the shared
// HandedOffSurfaces suite covers the cross-panel story.
//
// S14: the Model routing card's effective-model list is
// self-contained (the S10 card pattern) and reads the socket
// context, so the context is mocked with a capturable fake socket
// (the MachineCapabilityCard test pattern).

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

let mockSendMessage;
let mockWsRef;
let messageListeners;

beforeEach(() => {
  mockSendMessage = jest.fn();
  messageListeners = [];
  mockWsRef = {
    current: {
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners.push(cb);
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') {
          messageListeners = messageListeners.filter((l) => l !== cb);
        }
      }),
      readyState: 1,
    },
  };
  useAetherSocket.mockReturnValue({
    wsRef: mockWsRef,
    sendMessage: mockSendMessage,
    isConnected: true,
    reconnectCount: 0,
  });
});

function simulateMessage(data) {
  act(() => {
    for (const cb of [...messageListeners]) {
      cb({ data: JSON.stringify(data) });
    }
  });
}

const CONFIG = {
  supervisor_spawn_allowlist: ['planner'],
  supervisor_delegation_cap: 4,
  prewalk_agents: [],
  agent_output_schemas: {
    planner: { type: 'object', required: ['steps'] },
  },
  compaction_trigger_messages: 45,
  compaction_trigger_chars: 20000,
  compaction_keep_recent: 10,
};

// S21 (D2-38): the panel enumerates the engine's AGENT_LIST
// roster — the fixture is the handler's real payload shape
// (aether_engine/routes/websocket.py _handle_agent_list_request).
const AGENT_LIST_PAYLOAD = {
  agents: [
    { id: 'supervisor', name: 'Supervisor', description: 'Routes tasks and queries memory.', model: 'inherit', tools: null },
    { id: 'planner', name: 'Planner Agent', description: 'Breaks goals into structured steps.', model: 'inherit', tools: null },
    { id: 'coder', name: 'Coder Agent', description: 'Writes, tests, and debugs code.', model: 'google_gemini:gemini-3.5-flash', tools: null },
    { id: 'researcher', name: 'Researcher Agent', description: 'Gathers information and synthesizes findings.', model: 'inherit', tools: null },
  ],
};

function deliverAgentList() {
  simulateMessage({ type: 'AGENT_LIST_RESPONSE', payload: AGENT_LIST_PAYLOAD });
}

function renderPanel(config = CONFIG, configError = '') {
  const onConfigUpdate = jest.fn();
  render(
    <OrchestrationPanel config={config} configError={configError} onConfigUpdate={onConfigUpdate} />
  );
  // S21: deliver the roster before asserting on the cards that
  // enumerate it (they render a load note until it lands).
  deliverAgentList();
  return { onConfigUpdate };
}

describe('OrchestrationPanel', () => {
  test('renders the applied config in every control', () => {
    renderPanel();
    expect(screen.getByTestId('spawn-restrict-toggle')).toBeChecked();
    expect(screen.getByTestId('spawn-allow-planner')).toBeChecked();
    expect(screen.getByTestId('spawn-allow-researcher')).not.toBeChecked();
    expect(screen.getByTestId('spawn-allow-coder')).not.toBeChecked();
    expect(screen.getByTestId('spawn-allow-coder')).not.toBeDisabled();
    expect(screen.getByTestId('delegation-cap-input')).toHaveValue(4);
    expect(screen.getByTestId('prewalk-researcher')).not.toBeChecked();
    expect(screen.getByTestId('schema-planner')).toHaveValue(
      JSON.stringify(CONFIG.agent_output_schemas.planner, null, 2));
    expect(screen.getByTestId('schema-coder')).toHaveValue('');
    expect(screen.getByTestId('compaction-trigger-messages')).toHaveValue(45);
    expect(screen.getByTestId('compaction-trigger-chars')).toHaveValue(20000);
    expect(screen.getByTestId('compaction-keep-recent')).toHaveValue(10);
  });

  test('an unrestricted config leaves the toggle off and the boxes disabled', () => {
    renderPanel({ ...CONFIG, supervisor_spawn_allowlist: null });
    expect(screen.getByTestId('spawn-restrict-toggle')).not.toBeChecked();
    expect(screen.getByTestId('spawn-allow-planner')).toBeDisabled();
    expect(screen.getByTestId('spawn-allow-researcher')).toBeDisabled();
  });

  test('turning the restriction on reports the full canonical allowlist', () => {
    const { onConfigUpdate } = renderPanel({ ...CONFIG, supervisor_spawn_allowlist: null });
    fireEvent.click(screen.getByTestId('spawn-restrict-toggle'));
    // S21: the canonical order is the engine-reported roster
    // order (planner, coder, researcher), not the old
    // hard-coded trio's order.
    expect(onConfigUpdate).toHaveBeenCalledWith({
      supervisor_spawn_allowlist: ['planner', 'coder', 'researcher'],
    });
  });

  // S21 (D2-38): the roster is the engine's AGENT_LIST answer.
  test('the panel requests the agent list and renders the roster with pins', () => {
    renderPanel();
    expect(mockSendMessage).toHaveBeenCalledWith(
      'AGENT_LIST_REQUEST', expect.objectContaining({ payload: {} }));
    const roster = screen.getByTestId('orchestration-agent-roster');
    expect(roster).toHaveTextContent('Supervisor');
    expect(roster).toHaveTextContent('Routes tasks and queries memory.');
    expect(screen.getByTestId('orchestration-agent-coder')).toHaveTextContent('pinned to google_gemini:gemini-3.5-flash');
    expect(screen.getByTestId('orchestration-agent-planner')).toHaveTextContent('no Agents-tab pin');
    // The supervisor is in the roster but is NOT a spawn /
    // prewalk / schema target — it does the delegating.
    expect(screen.queryByTestId('spawn-allow-supervisor')).not.toBeInTheDocument();
    expect(screen.queryByTestId('prewalk-supervisor')).not.toBeInTheDocument();
    expect(screen.queryByTestId('schema-supervisor')).not.toBeInTheDocument();
  });

  test('before the list lands, the enumerating cards show a note, not guessed agents', () => {
    const onConfigUpdate = jest.fn();
    render(<OrchestrationPanel config={CONFIG} configError="" onConfigUpdate={onConfigUpdate} />);
    expect(screen.getAllByTestId('orchestration-agents-note').length).toBeGreaterThan(0);
    expect(screen.queryByTestId('spawn-allow-coder')).not.toBeInTheDocument();
    expect(screen.queryByTestId('orchestration-agent-roster')).not.toBeInTheDocument();
    // Non-enumerating controls are unaffected by the wait.
    expect(screen.getByTestId('delegation-cap-input')).toHaveValue(4);
  });

  test('an errored agent-list reply shows the error in the note', () => {
    const onConfigUpdate = jest.fn();
    render(<OrchestrationPanel config={CONFIG} configError="" onConfigUpdate={onConfigUpdate} />);
    simulateMessage({ type: 'AGENT_LIST_RESPONSE', payload: { error: 'agent registry unavailable' } });
    expect(screen.getAllByTestId('orchestration-agents-note')[0]).toHaveTextContent(
      'The engine could not report its agent list: agent registry unavailable');
    expect(screen.queryByTestId('spawn-allow-coder')).not.toBeInTheDocument();
  });

  test('the delegation cap reports a parsed integer on blur, including zero', () => {
    const { onConfigUpdate } = renderPanel();
    const cap = screen.getByTestId('delegation-cap-input');
    fireEvent.change(cap, { target: { value: '0' } });
    fireEvent.blur(cap);
    expect(onConfigUpdate).toHaveBeenCalledWith({ supervisor_delegation_cap: 0 });
  });

  test('an out-of-range cap is refused locally and reports nothing', () => {
    const { onConfigUpdate } = renderPanel();
    const cap = screen.getByTestId('delegation-cap-input');
    fireEvent.change(cap, { target: { value: '1001' } });
    fireEvent.blur(cap);
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(
      'Delegation cap must be a whole number between 0 and 1000.');
    expect(onConfigUpdate).not.toHaveBeenCalled();
  });

  test('a schema that parses to a non-object is refused locally', () => {
    const { onConfigUpdate } = renderPanel();
    fireEvent.change(screen.getByTestId('schema-coder'), { target: { value: '[1, 2]' } });
    fireEvent.click(screen.getByTestId('schema-save-coder'));
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(
      'Output schema for coder must be a JSON object.');
    expect(onConfigUpdate).not.toHaveBeenCalled();
  });

  test('compaction save reports the three thresholds as integers', () => {
    const { onConfigUpdate } = renderPanel();
    fireEvent.change(screen.getByTestId('compaction-trigger-messages'), { target: { value: '55' } });
    fireEvent.change(screen.getByTestId('compaction-trigger-chars'), { target: { value: '31000' } });
    fireEvent.change(screen.getByTestId('compaction-keep-recent'), { target: { value: '7' } });
    fireEvent.click(screen.getByTestId('compaction-save'));
    expect(onConfigUpdate).toHaveBeenCalledWith({
      compaction_trigger_messages: 55,
      compaction_trigger_chars: 31000,
      compaction_keep_recent: 7,
    });
  });

  test('an engine refusal passed down as configError is shown verbatim', () => {
    renderPanel(CONFIG, 'agent_output_schemas: not an object');
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(
      'agent_output_schemas: not an object');
  });

  // S13: bounded sub-agent spawning controls — rendered from the
  // applied config and persisted through the same onConfigUpdate
  // path (CONFIG_UPDATE with strict engine-side validation).
  const SUBAGENT_CONFIG = {
    ...CONFIG,
    subagents_enabled: true,
    subagents_max_per_turn: 3,
    subagent_timeout_s: 90,
    subagent_token_budget: 15000,
    subagent_cost_budget_usd: 0.5,
  };

  test('renders the applied sub-agent bounds', () => {
    renderPanel(SUBAGENT_CONFIG);
    expect(screen.getByTestId('subagents-enabled-toggle')).toBeChecked();
    expect(screen.getByTestId('subagents-max-per-turn')).toHaveValue(3);
    expect(screen.getByTestId('subagent-timeout-s')).toHaveValue(90);
    expect(screen.getByTestId('subagent-token-budget')).toHaveValue(15000);
    expect(screen.getByTestId('subagent-cost-budget')).toHaveValue(0.5);
  });

  test('a config without sub-agent fields renders the engine defaults', () => {
    renderPanel();
    expect(screen.getByTestId('subagents-enabled-toggle')).toBeChecked();
    expect(screen.getByTestId('subagents-max-per-turn')).toHaveValue(4);
    expect(screen.getByTestId('subagent-timeout-s')).toHaveValue(120);
    expect(screen.getByTestId('subagent-token-budget')).toHaveValue(20000);
    expect(screen.getByTestId('subagent-cost-budget')).toHaveValue(0);
  });

  test('the sub-agents toggle reports the boolean through onConfigUpdate', () => {
    const { onConfigUpdate } = renderPanel(SUBAGENT_CONFIG);
    fireEvent.click(screen.getByTestId('subagents-enabled-toggle'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ subagents_enabled: false });
  });

  test('sub-agent bounds report parsed values on blur', () => {
    const { onConfigUpdate } = renderPanel(SUBAGENT_CONFIG);
    const maxPerTurn = screen.getByTestId('subagents-max-per-turn');
    fireEvent.change(maxPerTurn, { target: { value: '0' } });
    fireEvent.blur(maxPerTurn);
    expect(onConfigUpdate).toHaveBeenCalledWith({ subagents_max_per_turn: 0 });

    const timeout = screen.getByTestId('subagent-timeout-s');
    fireEvent.change(timeout, { target: { value: '45' } });
    fireEvent.blur(timeout);
    expect(onConfigUpdate).toHaveBeenCalledWith({ subagent_timeout_s: 45 });

    const cost = screen.getByTestId('subagent-cost-budget');
    fireEvent.change(cost, { target: { value: '1.25' } });
    fireEvent.blur(cost);
    expect(onConfigUpdate).toHaveBeenCalledWith({ subagent_cost_budget_usd: 1.25 });
  });

  test('an out-of-range sub-agent bound is refused locally and reports nothing', () => {
    const { onConfigUpdate } = renderPanel(SUBAGENT_CONFIG);
    const timeout = screen.getByTestId('subagent-timeout-s');
    fireEvent.change(timeout, { target: { value: '0' } });
    fireEvent.blur(timeout);
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(
      'Sub-agent timeout (seconds) must be a whole number between 1 and 3600.');
    expect(onConfigUpdate).not.toHaveBeenCalled();
  });

  // S14: model routing — the mode toggle persists through the
  // same onConfigUpdate path, and the effective-model list renders
  // the engine's AGENT_ROUTING_RESPONSE verbatim (it computes
  // nothing itself).
  const ROUTING_REPORT = {
    mode: 'auto',
    agents: [
      { agent: 'supervisor', provider: 'google_gemini', model: 'gemini-3.5-flash', source: 'run_default', tier: null, reason: 'The supervisor runs on the run\u2019s model \u2014 it does the delegating, so auto routing chooses models for the specialists instead.' },
      { agent: 'researcher', provider: 'google_gemini', model: 'gemini-3.5-flash', source: 'auto', tier: 'small', reason: 'A standard task \u2014 your default model google_gemini:gemini-3.5-flash (small tier) fits.' },
      { agent: 'planner', provider: 'google_gemini', model: 'gemini-3.5-flash', source: 'auto', tier: 'small', reason: 'A standard task \u2014 your default model google_gemini:gemini-3.5-flash (small tier) fits.' },
      { agent: 'coder', provider: 'openai', model: 'gpt-4o', source: 'pinned', tier: null, reason: 'Pinned by you in Agents settings \u2014 routing never overrides a pin.' },
    ],
  };

  test('the model routing toggle renders the applied mode and reports changes', () => {
    const { onConfigUpdate } = renderPanel({ ...CONFIG, model_routing: 'auto' });
    expect(screen.getByTestId('model-routing-toggle')).toBeChecked();
    fireEvent.click(screen.getByTestId('model-routing-toggle'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ model_routing: 'off' });
  });

  test('a config without model_routing renders routing off (the engine default)', () => {
    const { onConfigUpdate } = renderPanel();
    expect(screen.getByTestId('model-routing-toggle')).not.toBeChecked();
    fireEvent.click(screen.getByTestId('model-routing-toggle'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ model_routing: 'auto' });
  });

  test('the effective-model list requests the report and renders it verbatim', () => {
    renderPanel({ ...CONFIG, model_routing: 'auto' });
    expect(mockSendMessage).toHaveBeenCalledWith(
      'AGENT_ROUTING_REQUEST', expect.objectContaining({ payload: {} }));
    simulateMessage({ type: 'AGENT_ROUTING_RESPONSE', payload: ROUTING_REPORT });
    const list = screen.getByTestId('model-routing-effective-list');
    expect(list).toBeInTheDocument();
    const coder = screen.getByTestId('model-routing-agent-coder');
    expect(coder).toHaveTextContent('coder');
    expect(coder).toHaveTextContent('openai:gpt-4o');
    expect(coder).toHaveTextContent('Pinned by you');
    expect(coder).toHaveTextContent('routing never overrides a pin');
    const researcher = screen.getByTestId('model-routing-agent-researcher');
    expect(researcher).toHaveTextContent('Auto routing');
    expect(researcher).toHaveTextContent('gemini-3.5-flash');
    expect(screen.getByTestId('model-routing-agent-supervisor')).toHaveTextContent('Run default');
  });
});
