/** Wave 5 (Annex C row 7): ToolsPanel extracted from
 * SettingsPanel.jsx as a controlled panel — the shell keeps the
 * approval/grants/tools state and the sends; these tests pin
 * the load states, the grants rendering and the revoke handler. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ToolsPanel from '../ToolsPanel';

function baseProps(overrides = {}) {
  return {
    approvalMode: 'ask',
    handleApprovalModeChange: jest.fn(),
    controlRefusal: '',
    approvalBindingMode: 'rebind',
    handleApprovalBindingChange: jest.fn(),
    approvalGrants: [],
    grantsAnswered: true,
    isConnected: true,
    pendingGrantRevokeFor: jest.fn(() => false),
    handleRevokeGrant: jest.fn(),
    configAnswered: true,
    engineConfig: { path_rules: [] },
    configUpdateError: '',
    onConfigUpdate: jest.fn(),
    tools: [],
    toolsAnswered: true,
    pendingToolPolicyFor: jest.fn(() => undefined),
    onToolPolicyChange: jest.fn(),
    ...overrides,
  };
}

describe('ToolsPanel', () => {
  test('before the answers land, grants and tools show load states (RUX-C-01)', () => {
    render(<ToolsPanel {...baseProps({ grantsAnswered: false, toolsAnswered: false })} />);
    expect(screen.getByTestId('approval-grants-load-state')).toBeInTheDocument();
    expect(screen.getByTestId('tools-load-state')).toBeInTheDocument();
    expect(screen.queryByText('No approval grants remembered.')).not.toBeInTheDocument();
    expect(screen.queryByText('No tools registered.')).not.toBeInTheDocument();
  });

  test('answered-empty renders the genuine empty states and the ask-mode description', () => {
    render(<ToolsPanel {...baseProps()} />);
    expect(screen.getByText('No approval grants remembered.')).toBeInTheDocument();
    expect(screen.getByText('No tools registered.')).toBeInTheDocument();
    expect(screen.getByText('Every non-read-only tool asks for approval before it runs.')).toBeInTheDocument();
  });

  test('a grant renders and Revoke fires the shell handler with its id', () => {
    const props = baseProps({
      approvalGrants: [{ id: 'g1', prefix: '/home/user/project', ceiling: 'write', tool_name: 'write_file' }],
    });
    render(<ToolsPanel {...props} />);
    expect(screen.getByText('/home/user/project')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Revoke'));
    expect(props.handleRevokeGrant).toHaveBeenCalledWith('g1');
  });

  test('a queued revoke replaces the button with its queued state (RUX-A-11)', () => {
    render(<ToolsPanel {...baseProps({
      approvalGrants: [{ id: 'g1', prefix: '/tmp/x', ceiling: 'read', tool_name: null }],
      pendingGrantRevokeFor: jest.fn(() => true),
    })} />);
    expect(screen.getByText('Revoke queued')).toBeInTheDocument();
    expect(screen.getByText('will apply when the engine reconnects')).toBeInTheDocument();
  });

  test('a tool row renders its name, description and default-policy note', () => {
    render(<ToolsPanel {...baseProps({
      tools: [{ function: { name: 'read_file', description: 'Reads a file' }, risk: 'read', policy: 'Require Approval', policy_is_default: true, effective_policy: 'ask', always_allow_permitted: true }],
    })} />);
    expect(screen.getByText('read_file')).toBeInTheDocument();
    expect(screen.getByText('Reads a file')).toBeInTheDocument();
    expect(screen.getByText(/Default policy — no override set/)).toBeInTheDocument();
  });
});
