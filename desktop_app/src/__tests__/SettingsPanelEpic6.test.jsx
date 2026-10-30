import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
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
  return { wsRef, getHandler: () => messageHandler };
}

describe('SettingsPanel (Epic 6, E6-06)', () => {
  it('search filters the section nav by label and keywords', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    const nav = within(screen.getByLabelText('Search settings').parentElement);
    expect(nav.getByText('API Providers')).toBeInTheDocument();
    expect(nav.getByText('Memory & Storage')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'episodes' } });
    // 'episodes' is a Memory keyword: only that section survives.
    expect(nav.queryByText('API Providers')).not.toBeInTheDocument();
    expect(nav.getByText('Memory & Storage')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'reset' } });
    expect(nav.getByText('Advanced')).toBeInTheDocument();
    expect(nav.queryByText('Memory & Storage')).not.toBeInTheDocument();
  });

  it('reset is two-step, sends CONFIG_RESET_REQUEST, and confirms on the response', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    fireEvent.click(screen.getByRole('button', { name: 'Reset all settings' }));
    // Armed, but nothing sent yet.
    expect(wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw).type)).not.toContain('CONFIG_RESET_REQUEST');
    fireEvent.click(screen.getByText('Click again to confirm reset'));
    expect(wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw).type)).toContain('CONFIG_RESET_REQUEST');
    act(() => {
      getHandler()({ data: JSON.stringify({ type: 'CONFIG_RESET_RESPONSE', payload: { success: true, config: {} } }) });
    });
    expect(screen.getByText('Settings were reset to defaults.')).toBeInTheDocument();
  });
});

describe('SettingsPanel skills toggle (Epic 7, E7-03)', () => {
  it('sends SKILL_TOGGLE_REQUEST and reflects the refreshed list', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Skills'));
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'SKILL_LIST_RESPONSE',
          payload: { skills: [{ name: 'alpha', description: 'A skill', source: 'local', slash_commands: ['/alpha'], required_tools: [], enabled: true }] },
        }),
      });
    });
    fireEvent.click(screen.getByText('Disable skill'));
    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
    const toggle = sent.find((m) => m.type === 'SKILL_TOGGLE_REQUEST');
    expect(toggle.payload).toEqual({ name: 'alpha', enabled: false });
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'SKILL_TOGGLE_RESPONSE',
          payload: { success: true, skills: [{ name: 'alpha', description: 'A skill', source: 'local', slash_commands: ['/alpha'], required_tools: [], enabled: false }] },
        }),
      });
    });
    expect(screen.getByText('Enable skill')).toBeInTheDocument();
    expect(screen.getByText(/Disabled — hidden from agents/)).toBeInTheDocument();
  });
});
