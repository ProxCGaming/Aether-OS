import React from 'react';
import { render, screen } from '@testing-library/react';
import Dashboard from '../Dashboard';

describe('Dashboard Component', () => {
  it('renders initializing state correctly', () => {
    render(<Dashboard status="Initializing" activeTasks={0} pluginCount={0} memoryMb={null} />);
    expect(screen.getByText('System Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Initializing')).toBeInTheDocument();
  });

  it('displays the correct number of active tasks', () => {
    render(<Dashboard status="Online" activeTasks={5} pluginCount={2} memoryMb={1024} />);
    expect(screen.getByText('5')).toBeInTheDocument();
  });
});

describe('Dashboard (Epic 6, E6-05)', () => {
  it('summarises provider health from the snapshot prop', () => {
    render(<Dashboard status="Online" providerHealth={{
      google_gemini: { status: 'healthy' },
      openai: { status: 'offline' },
      anthropic: { status: 'healthy' },
    }} />);
    expect(screen.getByText('Provider Health')).toBeInTheDocument();
    expect(screen.getByText('healthy: 2')).toBeInTheDocument();
    expect(screen.getByText('offline: 1')).toBeInTheDocument();
  });

  it('renders engine memory stats from the episodes response', () => {
    let handler = null;
    const wsRef = {
      current: {
        readyState: 1,
        send: jest.fn(),
        addEventListener: jest.fn((ev, cb) => { handler = cb; }),
        removeEventListener: jest.fn(),
      },
    };
    render(<Dashboard status="Online" wsRef={wsRef} />);
    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw).type);
    expect(sent).toContain('MEMORY_EPISODES_REQUEST');
    // eslint-disable-next-line testing-library/no-unnecessary-act
    const { act } = require('@testing-library/react');
    act(() => {
      handler({
        data: JSON.stringify({
          type: 'MEMORY_EPISODES_RESPONSE',
          payload: { backend: 'native', episodes: [], embedding_stats: { episodes: 12, embedded: 9, pending: 3 } },
        }),
      });
    });
    expect(screen.getByText(/Backend: native/)).toBeInTheDocument();
    expect(screen.getByText(/Episodes: 12 \(9 embedded, 3 pending\)/)).toBeInTheDocument();
  });
});

describe('Dashboard memory honesty (E4-F14 family)', () => {
  const makeWsRef = () => {
    const ref = { handler: null };
    ref.wsRef = {
      current: {
        readyState: 1,
        send: jest.fn(),
        addEventListener: jest.fn((ev, cb) => { ref.handler = cb; }),
        removeEventListener: jest.fn(),
      },
    };
    return ref;
  };
  const deliver = (handler, payload) => {
    // eslint-disable-next-line testing-library/no-unnecessary-act
    const { act } = require('@testing-library/react');
    act(() => {
      handler({ data: JSON.stringify({ type: 'MEMORY_EPISODES_RESPONSE', payload }) });
    });
  };

  it('surfaces an unavailable-backend error instead of native/zero stats', () => {
    const ref = makeWsRef();
    render(<Dashboard status="Online" wsRef={ref.wsRef} />);
    deliver(ref.handler, {
      episodes: [],
      error: 'The configured memory backend is unavailable (see engine log).',
    });
    // The failure is visible, in the engine's own words…
    expect(screen.getByText(/could not be loaded/)).toBeInTheDocument();
    expect(screen.getByText(/configured memory backend is unavailable/)).toBeInTheDocument();
    // …and the defaults are never dressed up as data.
    expect(screen.queryByText(/Backend:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Episodes:/)).not.toBeInTheDocument();
  });

  it('renders real stats on success (control)', () => {
    const ref = makeWsRef();
    render(<Dashboard status="Online" wsRef={ref.wsRef} />);
    deliver(ref.handler, {
      backend: 'langmem', episodes: [], embedding_stats: { episodes: 4, embedded: 4, pending: 0 },
    });
    expect(screen.getByText(/Backend: langmem/)).toBeInTheDocument();
    expect(screen.getByText(/Episodes: 4 \(4 embedded\)/)).toBeInTheDocument();
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument();
  });

  it('recovers to real stats when a later response succeeds', () => {
    const ref = makeWsRef();
    render(<Dashboard status="Online" wsRef={ref.wsRef} />);
    deliver(ref.handler, {
      episodes: [],
      error: 'The configured memory backend is unavailable (see engine log).',
    });
    expect(screen.getByText(/could not be loaded/)).toBeInTheDocument();
    deliver(ref.handler, {
      backend: 'native', episodes: [], embedding_stats: { episodes: 2, embedded: 1, pending: 1 },
    });
    expect(screen.getByText(/Backend: native/)).toBeInTheDocument();
    expect(screen.getByText(/Episodes: 2 \(1 embedded, 1 pending\)/)).toBeInTheDocument();
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument();
  });
});

describe('Dashboard metric honesty (RUX-C-14)', () => {
  it('unanswered metrics render as unknown (—), never as 0', () => {
    render(<Dashboard status="Initializing" activeTasks={0} pluginCount={null} providerHealth={null} />);
    // Active Tasks while offline and a not-yet-reported plugin
    // count are unknowns, not zeros.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('0 Active')).not.toBeInTheDocument();
    expect(screen.getByText(/Waiting for the engine/)).toBeInTheDocument();
    expect(screen.queryByText('No providers reporting yet')).not.toBeInTheDocument();
  });

  it('answered metrics render their real values', () => {
    render(
      <Dashboard
        status="Online"
        activeTasks={3}
        pluginCount={2}
        providerHealth={{}}
        providerHealthLoaded
      />,
    );
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('2 Active')).toBeInTheDocument();
    // An answered-but-empty health snapshot is genuinely "none reporting".
    expect(screen.getByText('No providers reporting yet')).toBeInTheDocument();
  });

  it('the memory card gains the slow notice instead of loading forever in silence', () => {
    jest.useFakeTimers();
    try {
      render(<Dashboard status="Online" />);
      expect(screen.getByText('Loading…')).toBeInTheDocument();
      expect(screen.queryByText(/taking longer than usual/)).not.toBeInTheDocument();
      const { act } = require('@testing-library/react');
      act(() => { jest.advanceTimersByTime(20000); });
      expect(screen.getByText(/taking longer than usual/)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});
