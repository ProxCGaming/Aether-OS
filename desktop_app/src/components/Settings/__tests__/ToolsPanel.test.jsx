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
    pendingToolEnabledFor: jest.fn(() => undefined),
    onToolEnabledChange: jest.fn(),
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

  describe('shared capability search/filter (D4-1)', () => {
    const READ = { function: { name: 'read_file', description: 'Reads a file' }, risk: 'read', policy: 'Require Approval', policy_is_default: true, effective_policy: 'ask', always_allow_permitted: true };
    const DENIED = { function: { name: 'wipe_disk', description: 'Erases a disk' }, risk: 'destructive', policy: 'Deny', policy_is_default: false, effective_policy: 'deny', always_allow_permitted: false };

    test('a name fragment narrows the list (the spec demo)', () => {
      render(<ToolsPanel {...baseProps({ tools: [READ, DENIED] })} />);
      fireEvent.change(screen.getByPlaceholderText('Search tools by name…'), { target: { value: 'wipe' } });
      expect(screen.getByText('wipe_disk')).toBeInTheDocument();
      expect(screen.queryByText('read_file')).not.toBeInTheDocument();
      expect(screen.getByText('Showing 1 of 2')).toBeInTheDocument();
    });

    test('the Disabled chip keys off the real Disable state, not the deny policy (D4-8)', () => {
      // A Deny-policy tool is still ENABLED (it exists; policy
      // refuses its calls) — the D4-1 placeholder that folded the
      // two together is gone. Only enabled === false is Disabled.
      const DISABLED = { function: { name: 'old_tool', description: 'Retired' }, risk: 'read', policy: 'Require Approval', policy_is_default: true, effective_policy: 'deny', always_allow_permitted: true, enabled: false };
      render(<ToolsPanel {...baseProps({ tools: [READ, DENIED, DISABLED] })} />);
      fireEvent.click(screen.getByText('Disabled · 1'));
      expect(screen.getByText('old_tool')).toBeInTheDocument();
      expect(screen.queryByText('wipe_disk')).not.toBeInTheDocument();
      expect(screen.queryByText('read_file')).not.toBeInTheDocument();
      fireEvent.click(screen.getByText('Error · 0'));
      expect(screen.getByTestId('tools-no-results')).toBeInTheDocument();
      fireEvent.click(screen.getByText('Show all tools'));
      expect(screen.getByText('read_file')).toBeInTheDocument();
      expect(screen.getByText('wipe_disk')).toBeInTheDocument();
    });
  });

  describe('real Disable + MCP source badge (D4-8 / D4-2)', () => {
    const MCP_TOOL = { function: { name: 'ext_search', description: 'Searches via a server' }, risk: 'read', policy: 'Require Approval', policy_is_default: true, effective_policy: 'ask', always_allow_permitted: true, source: 'mcp:extserver', enabled: true };
    const BUILTIN = { function: { name: 'read_file', description: 'Reads a file' }, risk: 'read', policy: 'Require Approval', policy_is_default: true, effective_policy: 'ask', always_allow_permitted: true, source: 'builtin', enabled: true };

    test('an MCP tool row carries its mcp:<server> source badge; a built-in carries none', () => {
      render(<ToolsPanel {...baseProps({ tools: [MCP_TOOL, BUILTIN] })} />);
      expect(screen.getByTestId('tool-source-ext_search')).toHaveTextContent('mcp:extserver');
      expect(screen.queryByTestId('tool-source-read_file')).not.toBeInTheDocument();
    });

    test('Disable fires the enabled handler with false; the row keeps its policy control', () => {
      const props = baseProps({ tools: [BUILTIN] });
      render(<ToolsPanel {...props} />);
      fireEvent.click(screen.getByTestId('tool-enabled-toggle-read_file'));
      expect(props.onToolEnabledChange).toHaveBeenCalledWith('read_file', false);
      // Disable ≠ policy: the approval dropdown is still there.
      expect(screen.getByText('Default policy — no override set. Pick one to override it.')).toBeInTheDocument();
    });

    test('a disabled tool offers Enable and explains itself without touching policy', () => {
      const props = baseProps({ tools: [{ ...BUILTIN, enabled: false }] });
      render(<ToolsPanel {...props} />);
      expect(screen.getByText(/Disabled — hidden from the model/)).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('tool-enabled-toggle-read_file'));
      expect(props.onToolEnabledChange).toHaveBeenCalledWith('read_file', true);
    });

    test('a queued disable makes the toggle inert and names the pending action', () => {
      const entry = { type: 'TOOL_ENABLED_SET_REQUEST', payload: { tool_name: 'read_file', enabled: false } };
      render(<ToolsPanel {...baseProps({
        tools: [BUILTIN],
        pendingToolEnabledFor: jest.fn((name) => (name === 'read_file' ? entry : undefined)),
      })} />);
      const toggle = screen.getByTestId('tool-enabled-toggle-read_file');
      expect(toggle).toBeDisabled();
      expect(screen.getByText('Disable queued — will apply when the engine reconnects')).toBeInTheDocument();
    });
  });
});
