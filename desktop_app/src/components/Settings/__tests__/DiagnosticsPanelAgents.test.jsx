import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DiagnosticsPanel from '../DiagnosticsPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

// Epic 1, E1-05: each agent row shows its EFFECTIVE model — the
// agent_models override when one is set, otherwise the global default
// it inherits. The per-agent selector itself already exists and is
// exercised by the AGENT_UPDATE_REQUEST flow.
describe('DiagnosticsPanel Agents effective model (Epic 1, E1-05)', () => {
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
    });
  });

  const emit = (obj) => {
    act(() => {
      listeners.message({ data: JSON.stringify(obj) });
    });
  };

  it('shows the inherit line for agents without an override and the override when set', () => {
    render(<DiagnosticsPanel activeMenu="Agents" />);
    expect(mockSendMessage).toHaveBeenCalledWith(
      'AGENT_LIST_REQUEST',
      expect.objectContaining({ payload: {} })
    );

    emit({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: {
        providers: [
          { name: 'openai', display_name: 'OpenAI', has_key: true, is_default: true, default_model: 'gpt-4o', models: ['gpt-4o', 'gpt-4'] },
          { name: 'anthropic', display_name: 'Anthropic', has_key: true, is_default: false, default_model: '', models: ['claude-sonnet-4'] },
        ],
      },
    });
    emit({
      type: 'AGENT_LIST_RESPONSE',
      payload: {
        agents: [
          { id: 'planner', name: 'Planner', description: 'Breaks goals into steps', model: 'inherit' },
          { id: 'coder', name: 'Coder', description: 'Writes code', model: 'anthropic:claude-sonnet-4' },
        ],
      },
    });

    expect(screen.getByTestId('effective-model-planner')).toHaveTextContent(
      'Effective model: Inherits OpenAI / gpt-4o'
    );
    expect(screen.getByTestId('effective-model-coder')).toHaveTextContent(
      'Effective model: claude-sonnet-4 (Anthropic)'
    );
  });

  it('falls back to a plain inherit line when no default provider is known yet', () => {
    render(<DiagnosticsPanel activeMenu="Agents" />);
    emit({
      type: 'AGENT_LIST_RESPONSE',
      payload: {
        agents: [
          { id: 'researcher', name: 'Researcher', description: 'Gathers facts', model: '' },
        ],
      },
    });
    expect(screen.getByTestId('effective-model-researcher')).toHaveTextContent(
      'Effective model: Inherits global default'
    );
  });
});
