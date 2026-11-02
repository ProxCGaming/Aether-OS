import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import Onboarding from '../Onboarding';
import { useAetherSocket } from '../../contexts/SocketContext';

jest.mock('../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

// E6-F03: onboarding steps must not advance past unacknowledged choices,
// and the disconnected state must be explained with recovery options.
// Pre-fix behaviour pinned by the audit: a workspace rejection left no
// error, no picker and an enabled Continue; the approval step advanced
// immediately and the summary claimed "Everything you chose is saved in
// the engine"; offline, approval showed "You're set" with zero packets
// acknowledged and the key-save sat at a bare "Saving…".
describe('Onboarding step acknowledgement and offline recovery (E6-F03)', () => {
  let mockSendMessage;
  let listeners;
  let socket;

  const providers = [{ name: 'google_gemini', display_name: 'Google Gemini', has_key: false }];
  const emit = (data) => act(() => listeners.forEach((cb) => cb({ data: JSON.stringify(data) })));

  beforeEach(() => {
    mockSendMessage = jest.fn();
    listeners = [];
    socket = {
      sendMessage: mockSendMessage,
      isConnected: true,
      wsRef: {
        current: {
          addEventListener: jest.fn((ev, cb) => { if (ev === 'message') listeners.push(cb); }),
          removeEventListener: jest.fn(),
          readyState: 1,
        },
      },
    };
    useAetherSocket.mockImplementation(() => socket);
    window.electronAPI = { showOpenDialog: jest.fn(async () => '/data/ws') };
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const toWorkspaceStep = () => {
    fireEvent.click(screen.getByText('Skip step'));
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
  };

  const toApprovalStep = () => {
    toWorkspaceStep();
    fireEvent.click(screen.getByText('Skip step'));
    expect(screen.getByText('Approval default')).toBeInTheDocument();
  };

  const pickFolder = async () => {
    await act(async () => {
      fireEvent.click(screen.getByText('Choose folder…'));
    });
    expect(mockSendMessage).toHaveBeenCalledWith('WORKSPACE_ADD_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ path: '/data/ws' }),
    }));
  };

  it('workspace rejection restores the picker, shows the error, and blocks Continue', async () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    toWorkspaceStep();
    await pickFolder();
    expect(screen.getByText('/data/ws')).toBeInTheDocument();

    emit({ type: 'WORKSPACE_ADD_RESPONSE', payload: { success: false, error: 'Path empty or already exists' } });

    // The refusal is surfaced, the picker is back, and Continue cannot
    // carry an unsaved selection into the next step.
    expect(screen.getByText(/could not be added/)).toBeInTheDocument();
    expect(screen.getByText(/already exists/)).toBeInTheDocument();
    expect(screen.getByText('Choose folder…')).toBeInTheDocument();
    expect(screen.getByText('Continue')).toBeDisabled();
    fireEvent.click(screen.getByText('Continue'));
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();

    // Recovery: picking again and being accepted advances as before.
    await pickFolder();
    emit({ type: 'WORKSPACE_ADD_RESPONSE', payload: { success: true, workspace: { path: '/data/ws' } } });
    expect(screen.getByText('Approval default')).toBeInTheDocument();
  });

  it('workspace success still auto-advances on the reply, as before', async () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    toWorkspaceStep();
    await pickFolder();
    emit({ type: 'WORKSPACE_ADD_RESPONSE', payload: { success: true, workspace: { path: '/data/ws' } } });
    expect(screen.getByText('Approval default')).toBeInTheDocument();
  });

  it('approval Continue waits for the engine reply before advancing', () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    toApprovalStep();
    fireEvent.click(screen.getByText('Continue'));
    expect(mockSendMessage).toHaveBeenCalledWith('APPROVAL_MODE_SET_REQUEST', expect.objectContaining({
      payload: { mode: 'ask' },
    }));

    // No reply yet: still on the step, no summary, Continue is inert.
    expect(screen.getByText('Approval default')).toBeInTheDocument();
    expect(screen.queryByText("You're set")).not.toBeInTheDocument();
    expect(screen.getByText('Saving…')).toBeDisabled();

    emit({ type: 'APPROVAL_MODE_SET_RESPONSE', payload: { success: true, mode: 'ask' } });
    expect(screen.getByText("You're set")).toBeInTheDocument();
    expect(screen.getByText(/Everything you chose is saved in the engine/)).toBeInTheDocument();
  });

  it('approval refusal surfaces an error and Continue retries', () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    toApprovalStep();
    fireEvent.click(screen.getByText('Continue'));
    emit({ type: 'APPROVAL_MODE_SET_RESPONSE', payload: { success: false, mode: 'ask' } });

    expect(screen.getByText(/could not be saved/)).toBeInTheDocument();
    expect(screen.getByText('Approval default')).toBeInTheDocument();
    expect(screen.getByText('Continue')).not.toBeDisabled();

    fireEvent.click(screen.getByText('Continue'));
    emit({ type: 'APPROVAL_MODE_SET_RESPONSE', payload: { success: true, mode: 'ask' } });
    expect(screen.getByText("You're set")).toBeInTheDocument();
  });

  it('offline approval explains the outage and never claims the choice is saved', () => {
    socket.isConnected = false;
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    expect(screen.getByText(/not connected to the engine/i)).toBeInTheDocument();

    fireEvent.click(screen.getByText('Skip step'));
    fireEvent.click(screen.getByText('Skip step'));
    expect(screen.getByText('Approval default')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Continue'));
    expect(mockSendMessage).toHaveBeenCalledWith('APPROVAL_MODE_SET_REQUEST', expect.anything());

    // The send is queued by the facade, but the step owns the truth:
    // pending, explained, with skip as the way out — no "You're set".
    expect(screen.getByText(/Queued — the engine is unreachable/)).toBeInTheDocument();
    expect(screen.getByText(/isn't saved yet/)).toBeInTheDocument();
    expect(screen.queryByText("You're set")).not.toBeInTheDocument();
    expect(screen.queryByText(/Everything you chose is saved/)).not.toBeInTheDocument();
    expect(screen.getByText('Skip setup')).toBeInTheDocument();
  });

  it('offline key-save explains the outage instead of a bare Saving…, then lands on reconnect', () => {
    socket.isConnected = false;
    const { rerender } = render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-123' } });
    fireEvent.click(screen.getByText('Save & continue'));

    expect(screen.getByText(/Queued — the engine is unreachable/)).toBeInTheDocument();
    expect(screen.getByText(/isn't saved yet/)).toBeInTheDocument();
    expect(screen.getByText('Skip step')).toBeInTheDocument();

    // The queued save is flushed by the facade on reconnect and its
    // acknowledgement advances the step exactly like an online save.
    socket.isConnected = true;
    rerender(<Onboarding providers={providers} onDone={jest.fn()} />);
    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: true } });
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
  });

  it('provider refusal restores the retry, as it already did', () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'bad-key' } });
    fireEvent.click(screen.getByText('Save & continue'));
    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: false } });

    expect(screen.getByText(/could not be saved/)).toBeInTheDocument();
    expect(screen.getByText('Save & continue')).not.toBeDisabled();

    fireEvent.click(screen.getByText('Save & continue'));
    expect(mockSendMessage.mock.calls.filter(([t]) => t === 'PROVIDER_SAVE_REQUEST')).toHaveLength(2);
    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: true } });
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
  });

  it('summary names a still-queued choice instead of claiming everything is saved', () => {
    socket.isConnected = false;
    const onDone = jest.fn();
    const { rerender } = render(<Onboarding providers={providers} onDone={onDone} />);

    // Queue a provider key save while offline, then skip past the step.
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-123' } });
    fireEvent.click(screen.getByText('Save & continue'));
    fireEvent.click(screen.getByText('Skip step'));
    fireEvent.click(screen.getByText('Skip step'));
    expect(screen.getByText('Approval default')).toBeInTheDocument();

    // Engine returns; the approval choice is acknowledged…
    socket.isConnected = true;
    rerender(<Onboarding providers={providers} onDone={onDone} />);
    fireEvent.click(screen.getByText('Continue'));
    emit({ type: 'APPROVAL_MODE_SET_RESPONSE', payload: { success: true, mode: 'ask' } });
    expect(screen.getByText("You're set")).toBeInTheDocument();

    // …but the provider key is still unacknowledged, so the summary
    // names it as queued instead of claiming a full save.
    expect(screen.queryByText(/Everything you chose is saved in the engine/)).not.toBeInTheDocument();
    const summary = screen.getByText(/your provider key/);
    expect(summary.textContent).toMatch(/queued/);
    expect(summary.textContent).toMatch(/not saved yet/);

    // When the queued save is finally acknowledged, the claim holds.
    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: true } });
    expect(screen.getByText(/Everything you chose is saved in the engine/)).toBeInTheDocument();
    expect(screen.getByText("You're set")).toBeInTheDocument();
  });
});
