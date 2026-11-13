/** Calendar layer C5: the Calendar & Reminders settings surface.
 * Load states are honest (loading/offline/error are not an empty
 * calendar), the toggles report edits through the controlled props,
 * and a REMINDER_DUE push becomes an in-app banner. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import CalendarPanel from '../CalendarPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeHarness({ isConnected = true } = {}) {
  let messageListeners = [];
  const sendMessage = jest.fn();
  const mockWsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners.push(cb);
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
      }),
    },
  };
  useAetherSocket.mockReturnValue({ wsRef: mockWsRef, sendMessage, isConnected });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { mockWsRef, sendMessage, simulateMessage };
}

function renderPanel(props = {}) {
  return render(
    <CalendarPanel
      configAnswered
      isConnected
      calendarEnabled
      onCalendarEnabledChange={jest.fn()}
      calendarDayBefore
      onCalendarDayBeforeChange={jest.fn()}
      calendarLeadMinutes={30}
      onCalendarLeadMinutesCommit={jest.fn()}
      {...props}
    />,
  );
}

describe('CalendarPanel (C5)', () => {
  test('requests the diary on mount and shows a loading state, not an empty calendar', () => {
    const { sendMessage } = makeHarness();
    renderPanel();
    expect(sendMessage).toHaveBeenCalledWith(
      'CALENDAR_LIST_REQUEST',
      expect.objectContaining({ payload: { limit: 20 } }),
    );
    expect(screen.getByTestId('calendar-events-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText(/No upcoming events/)).not.toBeInTheDocument();
  });

  test('renders events from the response', () => {
    const { simulateMessage } = makeHarness();
    renderPanel();
    simulateMessage({
      type: 'CALENDAR_LIST_RESPONSE',
      payload: {
        success: true,
        events: [{ id: 'e1', title: 'PTM', start: '2026-11-14T16:30:00+05:30' }],
      },
    });
    expect(screen.getByTestId('calendar-event-row')).toHaveTextContent('PTM');
    expect(screen.queryByTestId('calendar-events-load-state')).not.toBeInTheDocument();
  });

  test('an answered-empty reply shows the genuine empty text', () => {
    const { simulateMessage } = makeHarness();
    renderPanel();
    simulateMessage({ type: 'CALENDAR_LIST_RESPONSE', payload: { success: true, events: [] } });
    expect(screen.getByText(/No upcoming events/)).toBeInTheDocument();
  });

  test('a failed reply is an error state, not an empty calendar', () => {
    const { simulateMessage } = makeHarness();
    renderPanel();
    simulateMessage({
      type: 'CALENDAR_LIST_RESPONSE',
      payload: { success: false, error: 'db gone', events: [] },
    });
    const row = screen.getByTestId('calendar-events-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent('db gone');
  });

  test('toggles report edits through the controlled props', () => {
    makeHarness();
    const onCalendarEnabledChange = jest.fn();
    const onCalendarDayBeforeChange = jest.fn();
    renderPanel({ onCalendarEnabledChange, onCalendarDayBeforeChange });
    fireEvent.click(screen.getByTestId('calendar-enabled-toggle'));
    expect(onCalendarEnabledChange).toHaveBeenCalledWith(false);
    fireEvent.click(screen.getByTestId('calendar-day-before-toggle'));
    expect(onCalendarDayBeforeChange).toHaveBeenCalledWith(false);
  });

  test('lead minutes commit a clamped number on blur', () => {
    makeHarness();
    const onCalendarLeadMinutesCommit = jest.fn();
    renderPanel({ onCalendarLeadMinutesCommit });
    const input = screen.getByTestId('calendar-lead-minutes');
    fireEvent.change(input, { target: { value: '45' } });
    fireEvent.blur(input);
    expect(onCalendarLeadMinutesCommit).toHaveBeenCalledWith(45);
  });

  test('a REMINDER_DUE push shows the in-app banner', () => {
    const { simulateMessage } = makeHarness();
    renderPanel();
    simulateMessage({
      type: 'REMINDER_DUE',
      payload: { title: 'PTM', start_text: 'Sat 14 Nov, 04:30 PM', kind: 'lead' },
    });
    expect(screen.getByTestId('calendar-reminder-banner')).toHaveTextContent('PTM');
  });
});
