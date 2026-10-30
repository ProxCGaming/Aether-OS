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
