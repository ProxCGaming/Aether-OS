import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import StartupGate, { deriveStartupPhase, STARTUP_SLOW_NOTICE_MS, STARTUP_ESCALATION_MS } from '../components/StartupGate';

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
    // Well past the 120s health budget: slowness is not failure — the
    // heading and phase stay 'starting', no failure copy appears.
    act(() => { jest.advanceTimersByTime(300000); });
    expect(screen.getByText('Starting the engine…')).toBeInTheDocument();
    expect(screen.queryByText(/couldn’t start/)).not.toBeInTheDocument();
  });

  it('escalates to recovery actions only after the full health budget (S-10)', () => {
    jest.useFakeTimers();
    render(<StartupGate phase="starting" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    act(() => { jest.advanceTimersByTime(STARTUP_ESCALATION_MS - 1); });
    expect(screen.queryByText('Retry')).not.toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(1); });
    expect(screen.getByText(/longer than the engine’s full two-minute health budget/)).toBeInTheDocument();
    expect(screen.getByText('Retry')).toBeInTheDocument();
    expect(screen.getByText('Open engine log')).toBeInTheDocument();
    // Still the starting screen underneath: the engine is not dead.
    expect(screen.getByText('Starting the engine…')).toBeInTheDocument();
  });

  it('the escalated Retry and Open engine log actions are wired up', () => {
    jest.useFakeTimers();
    const onRetry = jest.fn();
    const onOpenLog = jest.fn();
    render(<StartupGate phase="starting" onRetry={onRetry} onOpenLog={onOpenLog} />);
    act(() => { jest.advanceTimersByTime(STARTUP_ESCALATION_MS); });
    fireEvent.click(screen.getByText('Retry'));
    expect(onRetry).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText('Open engine log'));
    expect(onOpenLog).toHaveBeenCalledTimes(1);
  });

  it('returning to starting after a retry restarts the escalation clock', () => {
    jest.useFakeTimers();
    const { rerender } = render(<StartupGate phase="starting" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    act(() => { jest.advanceTimersByTime(STARTUP_ESCALATION_MS); });
    expect(screen.getByText('Retry')).toBeInTheDocument();
    rerender(<StartupGate phase="failed" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    rerender(<StartupGate phase="starting" onRetry={jest.fn()} onOpenLog={jest.fn()} />);
    expect(screen.queryByText('Retry')).not.toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(STARTUP_ESCALATION_MS); });
    expect(screen.getByText('Retry')).toBeInTheDocument();
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
