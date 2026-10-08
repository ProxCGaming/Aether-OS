import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn()
  })
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
  return { wsRef, getHandler: () => messageHandler };
}

function feedTools(getHandler, tools) {
  act(() => {
    getHandler()({ data: JSON.stringify({ type: 'TOOL_LIST_RESPONSE', payload: { tools } }) });
  });
}

describe('SettingsPanel Tools Tab', () => {
  it('renders risk badges and disabled Always Allow notice for risky tools', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, [
      {
        function: { name: 'execute_shell', description: 'Run a local shell command.' },
        risk: 'execute',
        always_allow_permitted: false,
        effective_policy: 'ask',
        policy_is_default: true,
        policy: 'ask',
      },
      {
        function: { name: 'read_file', description: 'Read a file.' },
        risk: 'read_only',
        always_allow_permitted: true,
        effective_policy: 'allow',
        policy_is_default: true,
        policy: 'allow',
      },
    ]);

    // Risky tool gets the Risky badge; the read-only tool gets none.
    expect(screen.getByText('Risky')).toBeInTheDocument();
    expect(screen.getByText('Always Allow is disabled for risky tools.')).toBeInTheDocument();
    expect(screen.queryByText('Write')).not.toBeInTheDocument();
    // Both tools are listed by name.
    expect(screen.getByText('execute_shell')).toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
  });

  it('shows an empty state when no tools are registered', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, []);

    expect(screen.getByText('No tools registered.')).toBeInTheDocument();
  });

  it('renders the Write badge for write-risk tools', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Tools'));
    feedTools(getHandler, [
      {
        function: { name: 'write_file', description: 'Write a file.' },
        risk: 'write',
        always_allow_permitted: false,
        effective_policy: 'ask',
        policy_is_default: true,
        policy: 'ask',
      },
    ]);

    expect(screen.getByText('Write')).toBeInTheDocument();
  });
});

describe('SettingsPanel auxiliary models (Phase 9 UI honesty)', () => {
  it('renders only tasks with a real engine seam (Vision), no placebo dropdowns', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Models & Tasks'));
    expect(screen.getByText('⚙ Auxiliary models')).toBeInTheDocument();
    expect(screen.getByText('Vision')).toBeInTheDocument();

    // The six helper tasks with no engine call site must not be rendered.
    for (const label of ['Web extract', 'Compression', 'Skills hub', 'Approval', 'MCP', 'Title gen']) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it('Reset all to main clears the vision override over the socket', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);

    fireEvent.click(screen.getByText('Models & Tasks'));
    fireEvent.click(screen.getByText('Reset all to main'));

    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
    const reset = sent.find(m => m.type === 'AUXILIARY_MODEL_SET_REQUEST');
    expect(reset).toBeDefined();
    expect(reset.payload.task).toBe('vision');
    expect(reset.payload.model).toBe('auto');
  });
});
