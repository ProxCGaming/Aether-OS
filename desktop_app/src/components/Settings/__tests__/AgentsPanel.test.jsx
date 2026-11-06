/** Wave 5 (Annex C row 8): AgentsPanel extracted from
 * DiagnosticsPanel.jsx. The panel owns the Agents tab's socket
 * state on its own: agent/tool/provider list requests on mount
 * (D2-01), load states, and refused-update visibility (D2-02). */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import AgentsPanel from '../AgentsPanel';
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
  return { simulateMessage, sendMessage };
}

describe('AgentsPanel', () => {
  test('requests the agent, tool and provider lists on mount (D2-01)', () => {
    const { sendMessage } = makeHarness();
    render(<AgentsPanel />);
    expect(sendMessage).toHaveBeenCalledWith('AGENT_LIST_REQUEST', expect.objectContaining({ payload: {} }));
    expect(sendMessage).toHaveBeenCalledWith('TOOL_LIST_REQUEST', expect.objectContaining({ payload: {} }));
    expect(sendMessage).toHaveBeenCalledWith('PROVIDER_LIST_REQUEST', expect.objectContaining({ payload: {} }));
    expect(screen.getByTestId('agents-load-state')).toHaveAttribute('data-load-display', 'loading');
  });

  test('an errored agent list reply is the error state (RUX-C-10)', () => {
    const { simulateMessage } = makeHarness();
    render(<AgentsPanel />);
    simulateMessage({ type: 'AGENT_LIST_RESPONSE', payload: { error: 'registry unavailable' } });
    expect(screen.getByTestId('agents-load-state')).toHaveAttribute('data-load-display', 'error');
  });

  test('an inheriting agent shows the default provider model as its effective model (E1-05)', () => {
    const { simulateMessage } = makeHarness();
    render(<AgentsPanel />);
    simulateMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers: [{ name: 'openai', display_name: 'OpenAI', is_default: true, default_model: 'gpt-x', models: ['gpt-x'] }] },
    });
    simulateMessage({
      type: 'AGENT_LIST_RESPONSE',
      payload: { agents: [{ id: 'planner', name: 'Planner', description: 'Plans the work', model: 'inherit' }] },
    });
    expect(screen.getByText('Planner')).toBeInTheDocument();
    expect(screen.getByTestId('effective-model-planner')).toHaveTextContent('Effective model: Inherits OpenAI / gpt-x');
  });

  test('a refused agent update names the refusal (D2-02)', () => {
    const { simulateMessage } = makeHarness();
    render(<AgentsPanel />);
    simulateMessage({
      type: 'AGENT_UPDATE_RESPONSE',
      payload: { success: false, agent_id: 'planner', error: 'unknown provider' },
    });
    expect(screen.getByTestId('agent-update-error')).toHaveTextContent('Agent update refused: unknown provider');
  });
});
