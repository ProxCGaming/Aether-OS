import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import OrchestrationPanel from '../OrchestrationPanel';
import PathRulesPanel from '../PathRulesPanel';
import ProjectConfigPanel from '../ProjectConfigPanel';

// E6-F05: the handed-off panels are controlled — SettingsPanel owns
// the socket and the applied config; these tests pin the panels'
// rendering of an applied config and the exact partial-config
// payloads their edits report through onConfigUpdate / onSave.

const ORCH_CONFIG = {
  supervisor_spawn_allowlist: ['coder'],
  supervisor_delegation_cap: 3,
  prewalk_agents: ['planner'],
  agent_output_schemas: {
    coder: { type: 'object', required: ['summary'] },
  },
  compaction_trigger_messages: 50,
  compaction_trigger_chars: 30000,
  compaction_keep_recent: 9,
};

function renderOrchestration(config = ORCH_CONFIG, configError = '') {
  const onConfigUpdate = jest.fn();
  render(
    <OrchestrationPanel config={config} configError={configError} onConfigUpdate={onConfigUpdate} />
  );
  return { onConfigUpdate };
}

describe('OrchestrationPanel (E6-F05: Epic 1 handoff)', () => {
  test('reflects the engine config in every control', () => {
    renderOrchestration();
    expect(screen.getByTestId('spawn-restrict-toggle')).toBeChecked();
    expect(screen.getByTestId('spawn-allow-coder')).toBeChecked();
    expect(screen.getByTestId('spawn-allow-planner')).not.toBeChecked();
    expect(screen.getByTestId('spawn-allow-researcher')).not.toBeChecked();
    expect(screen.getByTestId('delegation-cap-input')).toHaveValue(3);
    expect(screen.getByTestId('prewalk-planner')).toBeChecked();
    expect(screen.getByTestId('prewalk-coder')).not.toBeChecked();
    expect(screen.getByTestId('compaction-trigger-messages')).toHaveValue(50);
    expect(screen.getByTestId('compaction-trigger-chars')).toHaveValue(30000);
    expect(screen.getByTestId('compaction-keep-recent')).toHaveValue(9);
    expect(screen.getByTestId('schema-coder')).toHaveValue(
      JSON.stringify(ORCH_CONFIG.agent_output_schemas.coder, null, 2));
    expect(screen.getByTestId('schema-planner')).toHaveValue('');
  });

  test('unchecking a specialist reports the reduced allowlist', () => {
    const { onConfigUpdate } = renderOrchestration();
    fireEvent.click(screen.getByTestId('spawn-allow-coder'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ supervisor_spawn_allowlist: [] });
  });

  test('checking a specialist reports the expanded allowlist in canonical order', () => {
    const { onConfigUpdate } = renderOrchestration();
    fireEvent.click(screen.getByTestId('spawn-allow-researcher'));
    expect(onConfigUpdate).toHaveBeenCalledWith({
      supervisor_spawn_allowlist: ['researcher', 'coder'],
    });
  });

  test('turning the restriction off reports null (all specialists allowed)', () => {
    const { onConfigUpdate } = renderOrchestration();
    fireEvent.click(screen.getByTestId('spawn-restrict-toggle'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ supervisor_spawn_allowlist: null });
  });

  test('an unrestricted config shows the toggle off and boxes disabled', () => {
    renderOrchestration({ ...ORCH_CONFIG, supervisor_spawn_allowlist: null });
    expect(screen.getByTestId('spawn-restrict-toggle')).not.toBeChecked();
    expect(screen.getByTestId('spawn-allow-coder')).toBeDisabled();
  });

  test('delegation cap reports on blur; invalid values report nothing', () => {
    const { onConfigUpdate } = renderOrchestration();
    const cap = screen.getByTestId('delegation-cap-input');
    fireEvent.change(cap, { target: { value: '5' } });
    fireEvent.blur(cap);
    expect(onConfigUpdate).toHaveBeenCalledWith({ supervisor_delegation_cap: 5 });
    fireEvent.change(cap, { target: { value: '-2' } });
    fireEvent.blur(cap);
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(/delegation cap/i);
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
  });

  test('prewalk toggles report the membership list', () => {
    const { onConfigUpdate } = renderOrchestration();
    fireEvent.click(screen.getByTestId('prewalk-coder'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ prewalk_agents: ['planner', 'coder'] });
    fireEvent.click(screen.getByTestId('prewalk-planner'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ prewalk_agents: ['coder'] });
  });

  test('compaction save reports the threshold triple as integers', () => {
    const { onConfigUpdate } = renderOrchestration();
    fireEvent.change(screen.getByTestId('compaction-trigger-messages'), { target: { value: '60' } });
    fireEvent.click(screen.getByTestId('compaction-save'));
    expect(onConfigUpdate).toHaveBeenCalledWith({
      compaction_trigger_messages: 60,
      compaction_trigger_chars: 30000,
      compaction_keep_recent: 9,
    });
  });

  test('compaction refuses a zero threshold locally', () => {
    const { onConfigUpdate } = renderOrchestration();
    fireEvent.change(screen.getByTestId('compaction-keep-recent'), { target: { value: '0' } });
    fireEvent.click(screen.getByTestId('compaction-save'));
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(/keep recent/i);
    expect(onConfigUpdate).not.toHaveBeenCalled();
  });

  test('schema save reports the parsed object; clearing removes the entry', () => {
    const { onConfigUpdate } = renderOrchestration();
    fireEvent.change(screen.getByTestId('schema-planner'), {
      target: { value: '{"type": "object", "required": ["plan"]}' },
    });
    fireEvent.click(screen.getByTestId('schema-save-planner'));
    expect(onConfigUpdate).toHaveBeenCalledWith({
      agent_output_schemas: {
        coder: ORCH_CONFIG.agent_output_schemas.coder,
        planner: { type: 'object', required: ['plan'] },
      },
    });
    fireEvent.change(screen.getByTestId('schema-coder'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('schema-save-coder'));
    expect(onConfigUpdate).toHaveBeenCalledWith({
      agent_output_schemas: { planner: { type: 'object', required: ['plan'] } },
    });
  });

  test('invalid schema JSON is refused locally and reports nothing', () => {
    const { onConfigUpdate } = renderOrchestration();
    fireEvent.change(screen.getByTestId('schema-coder'), { target: { value: '{nope' } });
    fireEvent.click(screen.getByTestId('schema-save-coder'));
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(/not valid JSON/i);
    expect(onConfigUpdate).not.toHaveBeenCalled();
  });

  test("an engine refusal passed down is shown verbatim", () => {
    renderOrchestration(ORCH_CONFIG, 'supervisor_delegation_cap: must be between 0 and 1000');
    expect(screen.getByTestId('orchestration-error')).toHaveTextContent(
      'supervisor_delegation_cap: must be between 0 and 1000');
  });
});

const RULES = [
  { prefix: '/home/u/projects', verdict: 'Always Allow' },
  { prefix: '/etc', verdict: 'Deny', tools: ['write_file'], risks: ['write'] },
];

function renderPathRules(rules = RULES, configError = '') {
  const onConfigUpdate = jest.fn();
  render(<PathRulesPanel rules={rules} configError={configError} onConfigUpdate={onConfigUpdate} />);
  return { onConfigUpdate };
}

describe('PathRulesPanel (E6-F05: Epic 2 handoff)', () => {
  test('renders one editor row per stored rule with its real shape', () => {
    renderPathRules();
    expect(screen.getByTestId('path-rule-prefix-0')).toHaveValue('/home/u/projects');
    expect(screen.getByTestId('path-rule-verdict-0')).toHaveValue('Always Allow');
    expect(screen.getByTestId('path-rule-tools-0')).toHaveValue('');
    expect(screen.getByTestId('path-rule-prefix-1')).toHaveValue('/etc');
    expect(screen.getByTestId('path-rule-verdict-1')).toHaveValue('Deny');
    expect(screen.getByTestId('path-rule-tools-1')).toHaveValue('write_file');
    expect(screen.getByTestId('path-rule-risks-1')).toHaveValue('write');
  });

  test('save reports cleaned rules — empty scopes are omitted, not stored empty', () => {
    const { onConfigUpdate } = renderPathRules();
    fireEvent.change(screen.getByTestId('path-rule-prefix-0'), {
      target: { value: '/home/u/work' },
    });
    fireEvent.click(screen.getByTestId('path-rules-save'));
    expect(onConfigUpdate).toHaveBeenCalledWith({
      path_rules: [
        { prefix: '/home/u/work', verdict: 'Always Allow' },
        { prefix: '/etc', verdict: 'Deny', tools: ['write_file'], risks: ['write'] },
      ],
    });
  });

  test('add and remove rows shape the next save', () => {
    const { onConfigUpdate } = renderPathRules();
    fireEvent.click(screen.getByTestId('path-rule-add'));
    fireEvent.change(screen.getByTestId('path-rule-prefix-2'), {
      target: { value: '/tmp/scratch' },
    });
    fireEvent.change(screen.getByTestId('path-rule-verdict-2'), {
      target: { value: 'Require Approval' },
    });
    fireEvent.click(screen.getByTestId('path-rule-remove-0'));
    fireEvent.click(screen.getByTestId('path-rules-save'));
    expect(onConfigUpdate).toHaveBeenCalledWith({
      path_rules: [
        { prefix: '/etc', verdict: 'Deny', tools: ['write_file'], risks: ['write'] },
        { prefix: '/tmp/scratch', verdict: 'Require Approval' },
      ],
    });
  });

  test('a rule without a prefix is refused locally', () => {
    const { onConfigUpdate } = renderPathRules();
    fireEvent.click(screen.getByTestId('path-rule-add'));
    fireEvent.click(screen.getByTestId('path-rules-save'));
    expect(screen.getByTestId('path-rules-error')).toHaveTextContent(/prefix/i);
    expect(onConfigUpdate).not.toHaveBeenCalled();
  });

  test('an empty rules list saves as an empty list', () => {
    const { onConfigUpdate } = renderPathRules([]);
    fireEvent.click(screen.getByTestId('path-rules-save'));
    expect(onConfigUpdate).toHaveBeenCalledWith({ path_rules: [] });
  });

  test("an engine refusal passed down is shown", () => {
    renderPathRules(RULES, 'path_rules[0]: unknown rule keys: bogus');
    expect(screen.getByTestId('path-rules-error')).toHaveTextContent(/unknown rule keys/);
  });
});

describe('ProjectConfigPanel (E6-F05: layered config)', () => {
  const baseProps = {
    workspaceRoot: '/ws',
    content: '{\n  "reasoning_effort": "high"\n}',
    exists: true,
    forbiddenKeys: ['path_rules', 'workspace_roots'],
    warning: '',
    status: '',
  };

  test('shows the layer file itself and the global-only warning', () => {
    render(<ProjectConfigPanel {...baseProps} onSave={jest.fn()} />);
    expect(screen.getByTestId('project-config-content')).toHaveValue(
      '{\n  "reasoning_effort": "high"\n}');
    expect(screen.getByTestId('project-config-root')).toHaveTextContent('/ws');
    expect(screen.getByTestId('project-config-forbidden')).toHaveTextContent(/path_rules/);
  });

  test('save reports the edited text through onSave', () => {
    const onSave = jest.fn();
    render(<ProjectConfigPanel {...baseProps} content="" exists={false} onSave={onSave} />);
    fireEvent.change(screen.getByTestId('project-config-content'), {
      target: { value: '{"compaction_trigger_messages": 7}' },
    });
    fireEvent.click(screen.getByTestId('project-config-save'));
    expect(onSave).toHaveBeenCalledWith('{"compaction_trigger_messages": 7}');
  });

  test('a saved status passed down is shown', () => {
    render(
      <ProjectConfigPanel
        {...baseProps}
        status="Saved — the layer applies to tasks run in this workspace."
        onSave={jest.fn()}
      />
    );
    expect(screen.getByTestId('project-config-status')).toHaveTextContent(/Saved/);
  });

  test('a corrupt-layer warning passed down is surfaced, editor stays enabled', () => {
    render(
      <ProjectConfigPanel
        {...baseProps}
        content="{broken"
        warning="Project config is not valid JSON; the engine ignores it."
        onSave={jest.fn()}
      />
    );
    expect(screen.getByTestId('project-config-warning')).toHaveTextContent(/not valid JSON/);
    expect(screen.getByTestId('project-config-content')).toHaveValue('{broken');
    expect(screen.getByTestId('project-config-save')).not.toBeDisabled();
  });

  test('without a workspace the editor explains and disables saving', () => {
    render(
      <ProjectConfigPanel
        {...baseProps}
        workspaceRoot=""
        content=""
        warning="No workspace is configured — add one in Projects first."
        onSave={jest.fn()}
      />
    );
    expect(screen.getByTestId('project-config-warning')).toHaveTextContent(/No workspace/);
    expect(screen.getByTestId('project-config-save')).toBeDisabled();
  });
});
