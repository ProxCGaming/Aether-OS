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

  // Add / cancel in-panel: the form calls the create contract
  // directly; a conflicts reply is a warning (nothing saved), and
  // "Save anyway" resends the same call with accept_conflicts set.
  const fillAddForm = () => {
    fireEvent.change(screen.getByTestId('calendar-add-title'), { target: { value: 'Dentist' } });
    fireEvent.change(screen.getByTestId('calendar-add-start'), { target: { value: '2026-11-20T22:00' } });
    fireEvent.change(screen.getByTestId('calendar-add-duration'), { target: { value: '45' } });
  };

  test('the add form sends title, start and duration_s to the create contract', () => {
    const { sendMessage } = makeHarness();
    renderPanel();
    fillAddForm();
    fireEvent.click(screen.getByTestId('calendar-add-submit'));
    const create = sendMessage.mock.calls
      .map((c) => c)
      .find((c) => c[0] === 'CALENDAR_CREATE_REQUEST');
    expect(create).toBeTruthy();
    expect(create[1].payload).toEqual({
      title: 'Dentist',
      start: new Date('2026-11-20T22:00').toISOString(),
      duration_s: 45 * 60,
    });
  });

  test('a conflicts reply warns, names the clash, and Save anyway resends with accept_conflicts', () => {
    const { sendMessage, simulateMessage } = makeHarness();
    renderPanel();
    fillAddForm();
    fireEvent.click(screen.getByTestId('calendar-add-submit'));
    simulateMessage({
      type: 'CALENDAR_CREATE_RESPONSE',
      payload: {
        success: true,
        event: null,
        conflicts: [{ event_id: 'e9', title: 'Film night', start: '2026-11-20T22:00:00+05:30', end: '2026-11-21T00:00:00+05:30' }],
      },
    });
    const warning = screen.getByTestId('calendar-conflict-warning');
    expect(warning).toHaveTextContent('Film night');
    expect(warning).toHaveTextContent(/Nothing has been saved yet/);
    // The form kept its values — the warning referred to them.
    expect(screen.getByTestId('calendar-add-title')).toHaveValue('Dentist');
    sendMessage.mockClear();
    fireEvent.click(screen.getByTestId('calendar-save-anyway'));
    const resent = sendMessage.mock.calls.find((c) => c[0] === 'CALENDAR_CREATE_REQUEST');
    expect(resent).toBeTruthy();
    expect(resent[1].payload).toEqual(expect.objectContaining({
      title: 'Dentist',
      duration_s: 45 * 60,
      accept_conflicts: true,
    }));
  });

  test('a clean create reply clears the form and refreshes the list', () => {
    const { sendMessage, simulateMessage } = makeHarness();
    renderPanel();
    fillAddForm();
    fireEvent.click(screen.getByTestId('calendar-add-submit'));
    sendMessage.mockClear();
    simulateMessage({
      type: 'CALENDAR_CREATE_RESPONSE',
      payload: { success: true, event: { id: 'e10', title: 'Dentist', start: '2026-11-20T16:30:00.000Z' }, conflicts: [] },
    });
    expect(screen.getByTestId('calendar-add-title')).toHaveValue('');
    expect(screen.queryByTestId('calendar-conflict-warning')).not.toBeInTheDocument();
    expect(sendMessage.mock.calls.some((c) => c[0] === 'CALENDAR_LIST_REQUEST')).toBe(true);
  });

  test('a failed create reply shows the error in the form', () => {
    const { simulateMessage } = makeHarness();
    renderPanel();
    fillAddForm();
    fireEvent.click(screen.getByTestId('calendar-add-submit'));
    simulateMessage({
      type: 'CALENDAR_CREATE_RESPONSE',
      payload: { success: false, error: 'store is read-only' },
    });
    expect(screen.getByTestId('calendar-add-error')).toHaveTextContent('store is read-only');
    expect(screen.queryByTestId('calendar-conflict-warning')).not.toBeInTheDocument();
  });

  test('an empty title is refused locally with a plain message', () => {
    const { sendMessage } = makeHarness();
    renderPanel();
    fireEvent.change(screen.getByTestId('calendar-add-start'), { target: { value: '2026-11-20T22:00' } });
    fireEvent.click(screen.getByTestId('calendar-add-submit'));
    expect(screen.getByTestId('calendar-add-error')).toHaveTextContent(/title/i);
    expect(sendMessage.mock.calls.some((c) => c[0] === 'CALENDAR_CREATE_REQUEST')).toBe(false);
  });

  const renderWithEvent = () => {
    const harness = makeHarness();
    renderPanel();
    harness.simulateMessage({
      type: 'CALENDAR_LIST_RESPONSE',
      payload: { success: true, events: [{ id: 'e1', title: 'PTM', start: '2026-11-14T16:30:00+05:30' }] },
    });
    return harness;
  };

  test('cancelling asks first, then sends the cancel with the event id', () => {
    window.confirm = jest.fn(() => true);
    const { sendMessage } = renderWithEvent();
    fireEvent.click(screen.getByTestId('calendar-cancel-e1'));
    expect(window.confirm).toHaveBeenCalled();
    const cancel = sendMessage.mock.calls.find((c) => c[0] === 'CALENDAR_CANCEL_REQUEST');
    expect(cancel).toBeTruthy();
    expect(cancel[1].payload).toEqual({ event_id: 'e1' });
  });

  test('declining the cancel confirmation sends nothing', () => {
    window.confirm = jest.fn(() => false);
    const { sendMessage } = renderWithEvent();
    fireEvent.click(screen.getByTestId('calendar-cancel-e1'));
    expect(window.confirm).toHaveBeenCalled();
    expect(sendMessage.mock.calls.some((c) => c[0] === 'CALENDAR_CANCEL_REQUEST')).toBe(false);
  });

  test('a not-found cancel reply is shown as a failure, never as success', () => {
    window.confirm = jest.fn(() => true);
    const { simulateMessage } = renderWithEvent();
    fireEvent.click(screen.getByTestId('calendar-cancel-e1'));
    simulateMessage({
      type: 'CALENDAR_CANCEL_RESPONSE',
      payload: { success: false, error: 'No event with that id.' },
    });
    expect(screen.getByTestId('calendar-action-error')).toHaveTextContent('No event with that id.');
    // The event is still listed — nothing pretended it went away.
    expect(screen.getByTestId('calendar-event-row')).toHaveTextContent('PTM');
  });

  test('a successful cancel refreshes the list', () => {
    window.confirm = jest.fn(() => true);
    const { sendMessage, simulateMessage } = renderWithEvent();
    sendMessage.mockClear();
    fireEvent.click(screen.getByTestId('calendar-cancel-e1'));
    simulateMessage({
      type: 'CALENDAR_CANCEL_RESPONSE',
      payload: { success: true, cancelled: 'e1' },
    });
    expect(screen.queryByTestId('calendar-action-error')).not.toBeInTheDocument();
    expect(sendMessage.mock.calls.some((c) => c[0] === 'CALENDAR_LIST_REQUEST')).toBe(true);
  });
});
