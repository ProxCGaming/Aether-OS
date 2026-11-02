import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

// E6-F05 wiring + socket round-trip: the handed-off surfaces live
// inside the existing sections — Orchestration under Agents, path
// rules under Tools, the project-layer editor under Advanced — and
// their edits flow through SettingsPanel's single CONFIG_UPDATE /
// applyConfigValues path. DiagnosticsPanel (the pre-existing Agents
// host) is stubbed: its own suites cover it.
jest.mock('../components/Settings/DiagnosticsPanel', () => () => (
  <div data-testid="diagnostics-stub" />
));
jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn(),
  }),
}));

function makeWsRef() {
  let messageHandler = null;
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => { messageHandler = handler; }),
      removeEventListener: jest.fn(),
    },
  };
  const sent = () => wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
  const deliver = (data) => {
    act(() => { messageHandler({ data: JSON.stringify(data) }); });
  };
  return { wsRef, getHandler: () => messageHandler, sent, deliver };
}

const FULL_CONFIG = {
  supervisor_spawn_allowlist: ['coder'],
  supervisor_delegation_cap: 3,
  prewalk_agents: ['planner'],
  agent_output_schemas: {},
  compaction_trigger_messages: 50,
  compaction_trigger_chars: 30000,
  compaction_keep_recent: 9,
  path_rules: [{ prefix: '/home/u/projects', verdict: 'Always Allow' }],
};

describe('SettingsPanel handed-off surfaces (E6-F05)', () => {
  it('Agents hosts Orchestration and round-trips config through the socket', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Agents'));
    expect(screen.getByTestId('diagnostics-stub')).toBeInTheDocument();
    expect(screen.getByText('Agent Orchestration')).toBeInTheDocument();
    // Opening Agents fetches the config for the controlled panel.
    expect(sent().map((m) => m.type)).toContain('CONFIG_GET_REQUEST');
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: FULL_CONFIG } });
    expect(screen.getByTestId('spawn-restrict-toggle')).toBeChecked();
    expect(screen.getByTestId('spawn-allow-coder')).toBeChecked();
    expect(screen.getByTestId('delegation-cap-input')).toHaveValue(3);
    // An edit goes out as a CONFIG_UPDATE with just the partial.
    fireEvent.click(screen.getByTestId('prewalk-coder'));
    const update = sent().find((m) => m.type === 'CONFIG_UPDATE_REQUEST');
    expect(update.payload.config).toEqual({ prewalk_agents: ['planner', 'coder'] });
    // A strict-field refusal is shown in the panel, verbatim.
    deliver({
      type: 'CONFIG_UPDATE_RESPONSE',
      payload: { success: false, error: 'prewalk_agents: every entry must be a non-empty string' },
    });
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(
      'prewalk_agents: every entry must be a non-empty string');
  });

  it('Tools hosts the path-rule editor, fed by the same config fetch', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Tools'));
    expect(screen.getByText('Path rules')).toBeInTheDocument();
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: FULL_CONFIG } });
    expect(screen.getByTestId('path-rule-prefix-0')).toHaveValue('/home/u/projects');
    fireEvent.change(screen.getByTestId('path-rule-prefix-0'), {
      target: { value: '/home/u/work' },
    });
    fireEvent.click(screen.getByTestId('path-rules-save'));
    const update = sent().find((m) => m.type === 'CONFIG_UPDATE_REQUEST');
    expect(update.payload.config).toEqual({
      path_rules: [{ prefix: '/home/u/work', verdict: 'Always Allow' }],
    });
  });

  it('Advanced hosts the project-layer editor with its own fetch/save flow', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    expect(screen.getByText('Project config layer')).toBeInTheDocument();
    expect(sent().map((m) => m.type)).toContain('PROJECT_CONFIG_GET_REQUEST');
    deliver({
      type: 'PROJECT_CONFIG_GET_RESPONSE',
      payload: {
        success: true, workspace_root: '/ws', exists: true,
        content: '{\n  "reasoning_effort": "high"\n}',
        config: { reasoning_effort: 'high' }, parse_error: null,
        forbidden_keys: ['path_rules'],
      },
    });
    expect(screen.getByTestId('project-config-content')).toHaveValue(
      '{\n  "reasoning_effort": "high"\n}');
    fireEvent.change(screen.getByTestId('project-config-content'), {
      target: { value: '{"compaction_trigger_messages": 7}' },
    });
    fireEvent.click(screen.getByTestId('project-config-save'));
    const update = sent().find((m) => m.type === 'PROJECT_CONFIG_UPDATE_REQUEST');
    expect(update.payload).toEqual({
      workspace_root: '/ws',
      content: '{"compaction_trigger_messages": 7}',
    });
    deliver({
      type: 'PROJECT_CONFIG_UPDATE_RESPONSE',
      payload: { success: true, workspace_root: '/ws', config: { compaction_trigger_messages: 7 } },
    });
    expect(screen.getByTestId('project-config-status')).toHaveTextContent(/Saved/);
    expect(screen.getByTestId('project-config-content')).toHaveValue(
      JSON.stringify({ compaction_trigger_messages: 7 }, null, 2));
  });
});
