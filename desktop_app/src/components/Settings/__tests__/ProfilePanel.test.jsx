/**
 * Profile as its own Settings section: the three profile
 * sections (About you / People / Preferences) read through
 * MEMORY_PROFILE_REQUEST with per-section paging, rows carry the
 * engine's source label verbatim plus importance/confidence, and
 * Edit / Delete reuse the existing fact action pairs — rows
 * change only on the engine's confirming reply.
 */
import React from 'react';
import { render, screen, fireEvent, act, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import ProfilePanel from '../ProfilePanel';
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

const PROFILE_REPLY = {
  type: 'MEMORY_PROFILE_RESPONSE',
  payload: {
    user: {
      items: [
        { fact_id: 'f-user-1', text: 'Lives in North Kolkata', entity_name: 'Arindam Roy', entity_type: 'person', provenance: 'stated', importance: 0.9, confidence: 0.98, timestamp: 1760000000, source_label: 'Set by you' },
      ],
      total: 1,
    },
    people: {
      items: [
        { fact_id: 'f-people-1', text: 'His mother; calls most evenings', entity_name: 'Maa', entity_type: 'person', provenance: 'inferred', importance: 0.8, confidence: 0.91, timestamp: 1760000100, source_label: 'From chat · 17 Nov' },
      ],
      total: 1,
    },
    preferences: {
      items: [
        { fact_id: 'f-pref-1', text: 'Prefers dark themes in every app', entity_name: 'Arindam Roy', entity_type: 'preference', provenance: 'stated', importance: 0.7, confidence: 0.76, timestamp: 1760000200, source_label: 'Set by you' },
      ],
      total: 1,
    },
  },
};

describe('ProfilePanel', () => {
  beforeEach(() => {
    window.confirm = jest.fn(() => true);
  });

  test('requests the profile on mount with per-section paging and shows load states until it answers', () => {
    const { sendMessage } = makeHarness();
    render(<ProfilePanel />);
    expect(sendMessage).toHaveBeenCalledWith(
      'MEMORY_PROFILE_REQUEST',
      expect.objectContaining({
        payload: {
          sections: {
            user: { limit: 20, offset: 0 },
            people: { limit: 20, offset: 0 },
            preferences: { limit: 20, offset: 0 },
          },
        },
      })
    );
    expect(screen.getByTestId('profile-user-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText('Lives in North Kolkata')).not.toBeInTheDocument();
  });

  test('renders the three sections with source labels verbatim and importance/confidence per row', () => {
    const { simulateMessage } = makeHarness();
    render(<ProfilePanel />);
    simulateMessage(PROFILE_REPLY);

    expect(screen.getByText('About you')).toBeInTheDocument();
    expect(screen.getByText('People')).toBeInTheDocument();
    expect(screen.getByText('Preferences')).toBeInTheDocument();

    expect(screen.getByText('Lives in North Kolkata')).toBeInTheDocument();
    expect(screen.getByTestId('profile-source-f-user-1')).toHaveTextContent('Set by you');
    expect(screen.getByTestId('profile-importance-f-user-1')).toHaveTextContent('importance 90%');
    expect(screen.getByTestId('profile-confidence-f-user-1')).toHaveTextContent('confidence 98%');

    // People are grouped under the person's name.
    expect(screen.getByText('Maa')).toBeInTheDocument();
    expect(screen.getByText('His mother; calls most evenings')).toBeInTheDocument();
    expect(screen.getByTestId('profile-source-f-people-1')).toHaveTextContent('From chat · 17 Nov');

    expect(screen.getByText('Prefers dark themes in every app')).toBeInTheDocument();
    expect(screen.getByTestId('profile-confidence-f-pref-1')).toHaveTextContent('confidence 76%');
  });

  test('accepts the people section in the grouped { name: [items] } shape', () => {
    const { simulateMessage } = makeHarness();
    render(<ProfilePanel />);
    simulateMessage({
      type: 'MEMORY_PROFILE_RESPONSE',
      payload: {
        user: { items: [], total: 0 },
        people: {
          'Kousik Dey': [
            { fact_id: 'f-kd-1', text: 'Mentor for semester 3, section 1', provenance: 'stated', importance: 0.6, confidence: 0.9, source_label: 'Set by you' },
          ],
        },
        preferences: { items: [], total: 0 },
      },
    });
    expect(screen.getByText('Kousik Dey')).toBeInTheDocument();
    expect(screen.getByText('Mentor for semester 3, section 1')).toBeInTheDocument();
  });

  test('an answered-empty section renders its empty state, not a load state', () => {
    const { simulateMessage } = makeHarness();
    render(<ProfilePanel />);
    simulateMessage({
      type: 'MEMORY_PROFILE_RESPONSE',
      payload: {
        user: { items: [], total: 0 },
        people: { items: [], total: 0 },
        preferences: { items: [], total: 0 },
      },
    });
    expect(screen.getByTestId('profile-user-empty')).toBeInTheDocument();
    expect(screen.getByText('Nothing about you yet')).toBeInTheDocument();
    expect(screen.getByText('No people yet')).toBeInTheDocument();
    expect(screen.getByText('No preferences yet')).toBeInTheDocument();
    expect(screen.queryByTestId('profile-user-load-state')).not.toBeInTheDocument();
  });

  test('a failed profile read is an error state, not an empty profile', () => {
    const { simulateMessage } = makeHarness();
    render(<ProfilePanel />);
    simulateMessage({
      type: 'MEMORY_PROFILE_RESPONSE',
      payload: { success: false, error: 'The profile store is unavailable.' },
    });
    expect(screen.getByTestId('profile-user-load-state')).toHaveAttribute('data-load-display', 'error');
    expect(screen.queryByText('Nothing about you yet')).not.toBeInTheDocument();
  });

  test('load more re-requests with a larger limit for that section only', () => {
    const { simulateMessage, sendMessage } = makeHarness();
    render(<ProfilePanel />);
    const many = Array.from({ length: 20 }, (_, i) => ({
      fact_id: `f-u-${i}`, text: `Fact ${i}`, entity_name: 'Arindam Roy', source_label: 'Set by you',
    }));
    simulateMessage({
      type: 'MEMORY_PROFILE_RESPONSE',
      payload: {
        user: { items: many, total: 25 },
        people: { items: [], total: 0 },
        preferences: { items: [], total: 0 },
      },
    });
    const moreButton = screen.getByTestId('profile-user-more');
    expect(moreButton).toHaveTextContent('5 more');
    fireEvent.click(moreButton);
    const profileCalls = sendMessage.mock.calls.filter((c) => c[0] === 'MEMORY_PROFILE_REQUEST');
    const lastPayload = profileCalls[profileCalls.length - 1][1].payload;
    expect(lastPayload.sections.user).toEqual({ limit: 40, offset: 0 });
    expect(lastPayload.sections.people).toEqual({ limit: 20, offset: 0 });
  });

  test('edit saves via MEMORY_FACT_UPDATE_REQUEST and a success re-reads the profile', async () => {
    const { simulateMessage, sendMessage } = makeHarness();
    render(<ProfilePanel />);
    simulateMessage(PROFILE_REPLY);

    const row = within(screen.getByTestId('profile-fact-f-user-1'));
    fireEvent.click(row.getByTitle('Edit this fact'));
    fireEvent.change(row.getByRole('textbox'), { target: { value: 'Lives in Barasat' } });
    fireEvent.click(row.getByText('Save'));

    expect(sendMessage).toHaveBeenCalledWith(
      'MEMORY_FACT_UPDATE_REQUEST',
      expect.objectContaining({ payload: { fact_id: 'f-user-1', fact_text: 'Lives in Barasat' } })
    );

    const callsBefore = sendMessage.mock.calls.filter((c) => c[0] === 'MEMORY_PROFILE_REQUEST').length;
    act(() => {
      simulateMessage({ type: 'MEMORY_FACT_UPDATE_RESPONSE', payload: { success: true } });
    });
    await waitFor(() => {
      expect(sendMessage.mock.calls.filter((c) => c[0] === 'MEMORY_PROFILE_REQUEST').length).toBe(callsBefore + 1);
    });
  });

  test('a rejected edit keeps the editor open and explains the rejection', () => {
    const { simulateMessage } = makeHarness();
    render(<ProfilePanel />);
    simulateMessage(PROFILE_REPLY);
    const row = within(screen.getByTestId('profile-fact-f-user-1'));
    fireEvent.click(row.getByTitle('Edit this fact'));
    fireEvent.click(row.getByText('Save'));
    act(() => {
      simulateMessage({ type: 'MEMORY_FACT_UPDATE_RESPONSE', payload: { success: false } });
    });
    expect(screen.getByText('The update was rejected — the fact was not changed.')).toBeInTheDocument();
    expect(screen.getByText('Save')).toBeInTheDocument();
  });

  test('delete asks first, then sends MEMORY_FACT_DELETE_REQUEST; declining sends nothing', () => {
    const { simulateMessage, sendMessage } = makeHarness();
    render(<ProfilePanel />);
    simulateMessage(PROFILE_REPLY);

    window.confirm = jest.fn(() => false);
    fireEvent.click(within(screen.getByTestId('profile-fact-f-user-1')).getByTitle('Delete this fact'));
    expect(window.confirm).toHaveBeenCalled();
    expect(sendMessage.mock.calls.filter((c) => c[0] === 'MEMORY_FACT_DELETE_REQUEST')).toHaveLength(0);

    window.confirm = jest.fn(() => true);
    fireEvent.click(within(screen.getByTestId('profile-fact-f-user-1')).getByTitle('Delete this fact'));
    expect(sendMessage).toHaveBeenCalledWith(
      'MEMORY_FACT_DELETE_REQUEST',
      expect.objectContaining({ payload: { fact_id: 'f-user-1' } })
    );
  });

  test('a failed delete keeps the row and surfaces the error', () => {
    const { simulateMessage } = makeHarness();
    render(<ProfilePanel />);
    simulateMessage(PROFILE_REPLY);
    fireEvent.click(within(screen.getByTestId('profile-fact-f-user-1')).getByTitle('Delete this fact'));
    act(() => {
      simulateMessage({ type: 'MEMORY_FACT_DELETE_RESPONSE', payload: { success: false, error: 'The fact is locked.' } });
    });
    expect(screen.getByText('The fact is locked.')).toBeInTheDocument();
    expect(screen.getByText('Lives in North Kolkata')).toBeInTheDocument();
  });
});
