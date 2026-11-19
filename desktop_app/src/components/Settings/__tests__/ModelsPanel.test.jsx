/** Wave 5 (Annex C row 7): ModelsPanel extracted from
 * SettingsPanel.jsx as a controlled panel — the shell keeps the
 * config state and the sends; these tests pin the panel's
 * rendering contract and its handler props. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ModelsPanel from '../ModelsPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

// The local tab's hub machinery is stubbed: these tests pin the
// panel's own composition, and the hub has its own suites. The
// socket context is mocked so the llama.cpp section (which owns
// its reads through it) can mount under the local tab.
jest.mock('../../../LocalModelsTab', () => {
  const ReactLib = require('react');
  return {
    __esModule: true,
    default: () => ReactLib.createElement('div', { 'data-testid': 'local-models-tab-stub' }),
  };
});

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function baseProps(overrides = {}) {
  return {
    providers: [
      { name: 'openai', display_name: 'OpenAI', has_key: true, is_default: true, default_model: 'gpt-x', models: ['gpt-x', 'gpt-y'] },
    ],
    wsRef: { current: null },
    safeSend: null,
    pendingMessages: [],
    reconnectCount: 0,
    isConnected: true,
    configAnswered: true,
    configLoadError: '',
    onRetryConfigLoad: jest.fn(),
    modelSwitchQueued: false,
    pendingModelSwitch: null,
    onModelDefaultChange: jest.fn(),
    reasoningEffort: 'standard',
    onReasoningEffortChange: jest.fn(),
    controlRefusal: '',
    auxModels: {},
    onAuxModelChange: jest.fn(),
    onAuxResetAll: jest.fn(),
    engineConfig: null,
    configUpdateError: '',
    onConfigUpdate: jest.fn(),
    ...overrides,
  };
}

describe('ModelsPanel', () => {
  test('before the config answer the tab shows its load state (RUX-C-01)', () => {
    render(<ModelsPanel {...baseProps({ configAnswered: false })} />);
    expect(screen.getByTestId('models-config-load-state')).toHaveAttribute('data-load-display', 'loading');
  });

  test('renders the global default, reasoning effort and the Vision auxiliary row', () => {
    render(<ModelsPanel {...baseProps()} />);
    expect(screen.queryByTestId('models-config-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('Global Default Model')).toBeInTheDocument();
    expect(screen.getByText('Reasoning Effort')).toBeInTheDocument();
    expect(screen.getByText('Vision')).toBeInTheDocument();
    expect(screen.getByText('auto · use main model')).toBeInTheDocument();
  });

  test('Reset all to main fires the shell handler', () => {
    const props = baseProps();
    render(<ModelsPanel {...props} />);
    fireEvent.click(screen.getByText('Reset all to main'));
    expect(props.onAuxResetAll).toHaveBeenCalledTimes(1);
  });

  test('a queued model switch is named under the dropdown', () => {
    render(<ModelsPanel {...baseProps({
      modelSwitchQueued: true,
      pendingModelSwitch: { type: 'MODEL_SET_DEFAULT', payload: { provider: 'openai', model: 'gpt-y' } },
    })} />);
    expect(screen.getByText(/Model switch to gpt-y queued/)).toBeInTheDocument();
  });

  test('a control refusal renders in the tab', () => {
    render(<ModelsPanel {...baseProps({ controlRefusal: 'The reasoning effort change was refused.' })} />);
    expect(screen.getByText('The reasoning effort change was refused.')).toBeInTheDocument();
  });

  test('the local tab mounts the llama.cpp router section under the hub', () => {
    const sendMessage = jest.fn();
    useAetherSocket.mockReturnValue({
      wsRef: { current: null },
      sendMessage,
      isConnected: true,
    });
    render(<ModelsPanel {...baseProps()} />);
    expect(screen.queryByTestId('llamacpp-panel')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Local Models'));
    expect(screen.getByTestId('local-models-tab-stub')).toBeInTheDocument();
    expect(screen.getByTestId('llamacpp-panel')).toBeInTheDocument();
    expect(sendMessage).toHaveBeenCalledWith(
      'LLAMACPP_STATUS_REQUEST',
      expect.objectContaining({ payload: {} }),
    );
    expect(sendMessage).toHaveBeenCalledWith(
      'LLAMACPP_MODELS_REQUEST',
      expect.objectContaining({ payload: {} }),
    );
  });
});
