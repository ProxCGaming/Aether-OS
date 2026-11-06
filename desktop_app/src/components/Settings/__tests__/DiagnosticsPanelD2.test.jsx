import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DiagnosticsPanel from '../DiagnosticsPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

// Wave 3, D2-01 / D2-02 / D2-18:
// - D2-01: the Agents view must re-request the provider list when
//   it mounts — socket messages are live-only, so a panel mounted
//   after App's connect-time broadcast otherwise offers "inherit"
//   and nothing else.
// - D2-02: per-agent model/tool updates are optimistic; an engine
//   refusal (AGENT_UPDATE_RESPONSE success:false) must roll the
//   control back to the confirmed value and say so.
// - D2-18: Add Server with a name but neither command nor URL must
//   explain itself instead of silently doing nothing.
describe('DiagnosticsPanel D2 fixes', () => {
  let mockSendMessage;
  let listeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
      isConnected: true,
    });
  });

  const emit = (obj) => {
    act(() => {
      listeners.message({ data: JSON.stringify(obj) });
    });
  };

  const sentTypes = () => mockSendMessage.mock.calls.map((c) => c[0]);

  const loadAgentsView = () => {
    render(<DiagnosticsPanel activeMenu="Agents" />);
    emit({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: {
        providers: [
          { name: 'openai', display_name: 'OpenAI', has_key: true, is_default: true, default_model: 'gpt-4o', models: ['gpt-4o', 'gpt-4'] },
        ],
      },
    });
    emit({
      type: 'AGENT_LIST_RESPONSE',
      payload: {
        agents: [
          { id: 'coder', name: 'Coder', description: 'Writes code', model: 'inherit' },
        ],
      },
    });
    emit({
      type: 'TOOL_LIST_RESPONSE',
      payload: {
        tools: [{ function: { name: 'read_file' } }, { function: { name: 'shell_exec' } }],
      },
    });
  };

  test('D2-01: mounting the Agents view requests the provider list', () => {
    render(<DiagnosticsPanel activeMenu="Agents" />);
    expect(sentTypes()).toContain('PROVIDER_LIST_REQUEST');
    expect(sentTypes()).toContain('AGENT_LIST_REQUEST');
  });

  test('D2-01: the per-agent picker offers the provider models once the list lands', () => {
    loadAgentsView();
    fireEvent.click(screen.getByText('[Use Global Default Model]'));
    expect(screen.getByText('gpt-4o (OpenAI)')).toBeInTheDocument();
  });

  test('D2-02: a refused model update rolls the picker back and shows the refusal', () => {
    loadAgentsView();
    fireEvent.click(screen.getByText('[Use Global Default Model]'));
    fireEvent.click(screen.getByText('gpt-4o (OpenAI)'));
    expect(sentTypes()).toContain('AGENT_UPDATE_REQUEST');
    // Optimistic: the effective-model line follows the pick.
    expect(screen.getByTestId('effective-model-coder')).toHaveTextContent('gpt-4o');

    emit({
      type: 'AGENT_UPDATE_RESPONSE',
      payload: { success: false, agent_id: 'coder', error: 'unknown provider: openai' },
    });
    expect(screen.getByTestId('effective-model-coder')).toHaveTextContent(/Inherits/);
    expect(screen.getByTestId('agent-update-error')).toHaveTextContent(/unknown provider: openai/);
  });

  test('D2-02: a confirmed model update keeps the value and shows no error', () => {
    loadAgentsView();
    fireEvent.click(screen.getByText('[Use Global Default Model]'));
    fireEvent.click(screen.getByText('gpt-4o (OpenAI)'));
    emit({
      type: 'AGENT_UPDATE_RESPONSE',
      payload: { success: true, agent_id: 'coder' },
    });
    expect(screen.getByTestId('effective-model-coder')).toHaveTextContent('gpt-4o');
    expect(screen.queryByTestId('agent-update-error')).not.toBeInTheDocument();
  });

  test('D2-02: a refused tools update re-checks the tool and shows the refusal', () => {
    loadAgentsView();
    const shellLabel = screen.getByText('shell_exec').closest('label');
    const shellCheckbox = shellLabel.querySelector('input[type="checkbox"]');
    expect(shellCheckbox).toBeChecked();
    fireEvent.click(shellCheckbox);
    expect(shellCheckbox).not.toBeChecked();

    emit({
      type: 'AGENT_UPDATE_RESPONSE',
      payload: { success: false, agent_id: 'coder', error: 'nothing to update' },
    });
    expect(shellCheckbox).toBeChecked();
    expect(screen.getByTestId('agent-update-error')).toHaveTextContent(/nothing to update/);
  });

  test('D2-18: Add Server with only a name explains what is missing and sends nothing', () => {
    render(<DiagnosticsPanel activeMenu="MCP" />);
    mockSendMessage.mockClear();
    fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
      target: { value: 'My Server' },
    });
    fireEvent.click(screen.getByText('Add Server'));
    expect(sentTypes()).not.toContain('MCP_SERVER_ADD_REQUEST');
    expect(screen.getByText(/command \(local server\) or a URL \(remote server\)/i)).toBeInTheDocument();
  });
});
