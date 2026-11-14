/** Master build S21 (R17 wiring): the model capability-check
 * card — Settings → Capabilities' sender for the scheduler's
 * CAPABILITY_CHECK_SET_SCHEDULE / RUN_NOW / GET_HISTORY events,
 * which worked engine-side with no UI. Pins: history is
 * requested on mount; the engine's summary (schedule, method,
 * last/next run, schedule_error) and history rows render
 * verbatim; schedule and method edits ride SET_SCHEDULE with
 * both fields; Run now rides RUN_NOW and the running state
 * clears only when a response carrying latest_run lands. */
import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import CapabilityCheckCard from '../CapabilityCheckCard';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn()
}));

const SUMMARY = {
  schedule: 'weekly',
  method: 'Both',
  schedule_error: null,
  last_run: {
    id: 'run-1', method: 'Both', started_at: 1700000000, finished_at: 1700000300,
    models_tested: 4, status: 'SUCCESS', cost_estimate: 0.12, error_log: null,
  },
  next_run_ts: 1700604800,
};

const HISTORY = [
  SUMMARY.last_run,
  {
    id: 'run-0', method: 'Option C', started_at: 1699395200, finished_at: 1699395500,
    models_tested: 4, status: 'FAILED', cost_estimate: 0.05,
    error_log: 'provider openai unreachable',
  },
];

describe('CapabilityCheckCard', () => {
  let mockSendMessage;
  let mockWsRef;
  let messageListeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    messageListeners = [];
    mockWsRef = {
      current: {
        addEventListener: jest.fn((event, cb) => {
          if (event === 'message') messageListeners.push(cb);
        }),
        removeEventListener: jest.fn((event, cb) => {
          if (event === 'message') {
            messageListeners = messageListeners.filter(l => l !== cb);
          }
        }),
        readyState: 1
      }
    };
    useAetherSocket.mockReturnValue({
      wsRef: mockWsRef,
      sendMessage: mockSendMessage,
      isConnected: true,
      reconnectCount: 0
    });
  });

  const simulateHistory = (payload) => {
    act(() => {
      messageListeners.forEach(cb => cb({
        data: JSON.stringify({ type: 'CAPABILITY_CHECK_HISTORY_RESPONSE', payload })
      }));
    });
  };

  test('requests history on mount and shows an asking state first', () => {
    render(<CapabilityCheckCard />);
    expect(mockSendMessage).toHaveBeenCalledWith(
      'CAPABILITY_CHECK_GET_HISTORY', expect.objectContaining({ payload: {} }));
    expect(screen.getByText(/Asking the engine for the check schedule/)).toBeInTheDocument();
  });

  test('renders the engine summary and history verbatim, failures included', async () => {
    render(<CapabilityCheckCard />);
    simulateHistory({ summary: SUMMARY, history: HISTORY });

    await waitFor(() => {
      expect(screen.getByTestId('capability-check-schedule')).toHaveValue('weekly');
    });
    expect(screen.getByTestId('capability-check-method')).toHaveValue('Both');
    expect(screen.getByTestId('capability-check-status')).toHaveTextContent('Last run:');
    expect(screen.getByTestId('capability-check-status')).toHaveTextContent('SUCCESS');
    expect(screen.getByTestId('capability-check-status')).toHaveTextContent('Next scheduled run:');

    const okRun = screen.getByTestId('capability-check-run-run-1');
    expect(okRun).toHaveTextContent('4 models tested');
    expect(okRun).toHaveTextContent('$0.12');
    const failedRun = screen.getByTestId('capability-check-run-run-0');
    expect(failedRun).toHaveTextContent('FAILED');
    expect(failedRun).toHaveTextContent('provider openai unreachable');
  });

  test('an empty history says so instead of inventing runs', async () => {
    render(<CapabilityCheckCard />);
    simulateHistory({
      summary: { schedule: 'none', method: 'Both', schedule_error: null, last_run: null, next_run_ts: null },
      history: [],
    });
    await waitFor(() => {
      expect(screen.getByTestId('capability-check-history-empty')).toBeInTheDocument();
    });
    expect(screen.getByTestId('capability-check-status')).toHaveTextContent('Last run: never');
    expect(screen.getByTestId('capability-check-status')).toHaveTextContent('No scheduled runs.');
  });

  test('a schedule the engine cannot honour surfaces its schedule_error', async () => {
    render(<CapabilityCheckCard />);
    simulateHistory({
      summary: { schedule: 'weekly', method: 'Both', schedule_error: 'bad cron', last_run: null, next_run_ts: null },
      history: [],
    });
    await waitFor(() => {
      expect(screen.getByTestId('capability-check-schedule-error')).toHaveTextContent('bad cron');
    });
  });

  test('schedule and method edits ride SET_SCHEDULE with both fields', async () => {
    render(<CapabilityCheckCard />);
    simulateHistory({ summary: SUMMARY, history: HISTORY });
    await waitFor(() => {
      expect(screen.getByTestId('capability-check-schedule')).toHaveValue('weekly');
    });

    fireEvent.change(screen.getByTestId('capability-check-schedule'), { target: { value: 'daily' } });
    expect(mockSendMessage).toHaveBeenCalledWith('CAPABILITY_CHECK_SET_SCHEDULE', expect.objectContaining({
      payload: { schedule: 'daily', method: 'Both' }
    }));

    fireEvent.change(screen.getByTestId('capability-check-method'), { target: { value: 'Option A' } });
    expect(mockSendMessage).toHaveBeenCalledWith('CAPABILITY_CHECK_SET_SCHEDULE', expect.objectContaining({
      payload: { schedule: 'daily', method: 'Option A' }
    }));
  });

  test('a stored cron schedule shows the cron field with the engine\u2019s value', async () => {
    render(<CapabilityCheckCard />);
    simulateHistory({
      summary: { schedule: '0 3 * * 1', method: 'Both', schedule_error: null, last_run: null, next_run_ts: null },
      history: [],
    });
    await waitFor(() => {
      expect(screen.getByTestId('capability-check-schedule')).toHaveValue('custom');
    });
    expect(screen.getByTestId('capability-check-cron')).toHaveValue('0 3 * * 1');
    fireEvent.change(screen.getByTestId('capability-check-cron'), { target: { value: '30 4 * * 2' } });
    fireEvent.click(screen.getByTestId('capability-check-cron-apply'));
    expect(mockSendMessage).toHaveBeenCalledWith('CAPABILITY_CHECK_SET_SCHEDULE', expect.objectContaining({
      payload: { schedule: '30 4 * * 2', method: 'Both' }
    }));
  });

  test('Run now rides RUN_NOW and clears only when a latest_run answer lands', async () => {
    render(<CapabilityCheckCard />);
    simulateHistory({ summary: SUMMARY, history: HISTORY });
    await waitFor(() => {
      expect(screen.getByTestId('capability-check-run-now')).not.toBeDisabled();
    });

    fireEvent.click(screen.getByTestId('capability-check-run-now'));
    expect(mockSendMessage).toHaveBeenCalledWith('CAPABILITY_CHECK_RUN_NOW', expect.objectContaining({
      payload: { method: 'Both' }
    }));
    expect(screen.getByTestId('capability-check-run-now')).toHaveTextContent('Running…');

    // A plain history answer (no latest_run) does not clear it.
    simulateHistory({ summary: SUMMARY, history: HISTORY });
    expect(screen.getByTestId('capability-check-run-now')).toHaveTextContent('Running…');

    const newRun = {
      id: 'run-2', method: 'Both', started_at: 1700001000, finished_at: 1700001400,
      models_tested: 4, status: 'SUCCESS', cost_estimate: 0.1, error_log: null,
    };
    simulateHistory({ summary: SUMMARY, history: [newRun, ...HISTORY], latest_run: newRun });
    await waitFor(() => {
      expect(screen.getByTestId('capability-check-run-now')).toHaveTextContent('Run check now');
    });
    expect(screen.getByTestId('capability-check-latest-run-2')).toBeInTheDocument();
  });
});
