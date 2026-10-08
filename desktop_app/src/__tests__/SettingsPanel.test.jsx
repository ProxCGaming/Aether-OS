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
