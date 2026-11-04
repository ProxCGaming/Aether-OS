/** Wave 3 slice 1 (RUX-C-01 umbrella + RUX-C-06): Settings sections
 * that load data on open show a load state until their first
 * answer — the false empties Lane C catalogued ("No tools
 * registered.", "No approval grants remembered.", "No skills
 * found…", config defaults) render only behind a real reply. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn(),
  }),
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
  const feed = (type, payload) => {
    act(() => {
      messageHandler({ data: JSON.stringify({ type, payload }) });
    });
  };
  return { wsRef, feed };
}

describe('SettingsPanel load states (RUX-C-01/C-06)', () => {
  test('Tools: load states before the replies; genuine empties only after answered-empty replies', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Tools'));

    expect(screen.getByTestId('tools-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.getByTestId('approval-grants-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText('No tools registered.')).not.toBeInTheDocument();
    expect(screen.queryByText('No approval grants remembered.')).not.toBeInTheDocument();

    feed('TOOL_LIST_RESPONSE', { tools: [] });
    expect(screen.queryByTestId('tools-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('No tools registered.')).toBeInTheDocument();

    feed('APPROVAL_GRANTS_RESPONSE', { grants: [] });
    expect(screen.queryByTestId('approval-grants-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('No approval grants remembered.')).toBeInTheDocument();
  });

  test('Tools: while the socket is down the load states are the offline variant', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} isConnected={false} />);
    fireEvent.click(screen.getByText('Tools'));
    expect(screen.getByTestId('tools-load-state')).toHaveAttribute('data-load-display', 'offline');
    expect(screen.getByTestId('tools-load-state')).toHaveTextContent(/Waiting for the engine/);
    expect(screen.queryByText('No tools registered.')).not.toBeInTheDocument();
  });

  test('Skills: load state before the reply; the create-a-skill invitation only after an answered empty', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Skills'));
    expect(screen.getByTestId('skills-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText(/No skills found in workspace/)).not.toBeInTheDocument();
    feed('SKILL_LIST_RESPONSE', { skills: [] });
    expect(screen.queryByTestId('skills-load-state')).not.toBeInTheDocument();
    expect(screen.getByText(/No skills found in workspace/)).toBeInTheDocument();
  });

  test('Voice: the config banner shows until the first config answer, then clears', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Voice Tuning'));
    expect(screen.getByTestId('voice-config-load-state')).toHaveAttribute('data-load-display', 'loading');
    feed('CONFIG_GET_RESPONSE', { config: {} });
    expect(screen.queryByTestId('voice-config-load-state')).not.toBeInTheDocument();
  });

  test('Providers: App\'s loaded signal threads through — loading row before, empty state after', () => {
    const { wsRef } = makeWsRef();
    const { rerender } = render(
      <SettingsPanel wsRef={wsRef} providers={[]} providersLoaded={false} localModels={[]} />
    );
    expect(screen.getByTestId('providers-load-state')).toHaveAttribute('data-load-display', 'loading');
    rerender(
      <SettingsPanel wsRef={wsRef} providers={[]} providersLoaded={true} localModels={[]} />
    );
    expect(screen.queryByTestId('providers-load-state')).not.toBeInTheDocument();
    expect(screen.getByTestId('providers-empty')).toBeInTheDocument();
  });
});
