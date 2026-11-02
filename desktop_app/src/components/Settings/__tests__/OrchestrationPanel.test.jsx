import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import OrchestrationPanel from '../OrchestrationPanel';

// OrchestrationPanel (E6-F05) is controlled: SettingsPanel owns the
// socket and the applied config; the panel renders the applied
// config and reports edits as partial-config payloads through
// onConfigUpdate. These tests pin the per-panel contract the commit
// gate requires (a <Name>.test.jsx per component); the shared
// HandedOffSurfaces suite covers the cross-panel story.

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

function renderPanel(config = CONFIG, configError = '') {
  const onConfigUpdate = jest.fn();
  render(
    <OrchestrationPanel config={config} configError={configError} onConfigUpdate={onConfigUpdate} />
  );
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
    expect(onConfigUpdate).toHaveBeenCalledWith({
      supervisor_spawn_allowlist: ['researcher', 'planner', 'coder'],
    });
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
});
