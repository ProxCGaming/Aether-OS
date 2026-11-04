import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import StartupGate, { deriveStartupPhase, STARTUP_SLOW_NOTICE_MS } from '../components/StartupGate';

// Wave 1 slice 4: the startup gate's state machine and screen.
// App-level behaviour (gate replacing the shell, onboarding after the
// gate, retry flow, ready latch) is covered in StartupGateApp.test.jsx.

describe('deriveStartupPhase', () => {
  it('is starting when nothing has been decided yet', () => {
    expect(deriveStartupPhase({})).toBe('starting');
    expect(deriveStartupPhase({ engineState: null })).toBe('starting');
    expect(deriveStartupPhase({ engineState: 'starting' })).toBe('starting');
    expect(deriveStartupPhase({ engineState: 'started' })).toBe('starting');
  });

  it('is failed only on the definitive failure signal', () => {
    expect(deriveStartupPhase({ engineState: 'failed' })).toBe('failed');
  });

  it('ready wins over a failure signal (an engine that answers anyway passes)', () => {
    expect(deriveStartupPhase({ ready: true, engineState: 'failed' })).toBe('ready');
    expect(deriveStartupPhase({ passed: true, engineState: 'failed' })).toBe('ready');
  });

  it('the latch keeps it ready after a later drop', () => {
    expect(deriveStartupPhase({ passed: true, ready: false, engineState: 'failed' })).toBe('ready');
  });
});

describe('StartupGate screen', () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  it('shows the starting state with the engine copy and no failure actions', () => {
    render(<StartupGate phase="starting" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    expect(screen.getByTestId('startup-gate')).toBeInTheDocument();
    expect(screen.getByText('Starting the engine…')).toBeInTheDocument();
    expect(screen.queryByText('Retry')).not.toBeInTheDocument();
    expect(screen.queryByText(/couldn’t start/)).not.toBeInTheDocument();
  });

  it('adds the cold-start reassurance only after the slow-notice delay', () => {
    jest.useFakeTimers();
    render(<StartupGate phase="starting" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    expect(screen.queryByText(/Cold starts can take a while/)).not.toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(STARTUP_SLOW_NOTICE_MS); });
    expect(screen.getByText(/Cold starts can take a while/)).toBeInTheDocument();
  });

  it('elapsed time alone NEVER turns starting into failed', () => {
    jest.useFakeTimers();
    render(<StartupGate phase="starting" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    // Well past the 120s health budget: slowness is not failure.
    act(() => { jest.advanceTimersByTime(300000); });
    expect(screen.getByText('Starting the engine…')).toBeInTheDocument();
    expect(screen.queryByText('Retry')).not.toBeInTheDocument();
    expect(screen.queryByText(/couldn’t start/)).not.toBeInTheDocument();
  });

  it('shows the failed state with Retry and Open engine log actions wired up', () => {
    const onRetry = jest.fn();
    const onOpenLog = jest.fn();
    render(<StartupGate phase="failed" onRetry={onRetry} onOpenLog={onOpenLog} />);
    expect(screen.getByText(/The engine couldn’t start/)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Retry'));
    expect(onRetry).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText('Open engine log'));
    expect(onOpenLog).toHaveBeenCalledTimes(1);
  });

  it('returning to starting after a retry restarts the slow-notice clock', () => {
    jest.useFakeTimers();
    const { rerender } = render(<StartupGate phase="failed" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    rerender(<StartupGate phase="starting" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    expect(screen.getByText('Starting the engine…')).toBeInTheDocument();
    expect(screen.queryByText(/Cold starts can take a while/)).not.toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(STARTUP_SLOW_NOTICE_MS); });
    expect(screen.getByText(/Cold starts can take a while/)).toBeInTheDocument();
  });
});
