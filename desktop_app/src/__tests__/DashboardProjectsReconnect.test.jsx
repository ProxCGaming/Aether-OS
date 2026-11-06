import React from 'react';
import { render } from '@testing-library/react';
import '@testing-library/jest-dom';
import Dashboard from '../Dashboard';
import Projects from '../Projects';

// Wave 3, D2-09: Dashboard and Projects fetched once on mount
// (deps [wsRef] only — the facade object is stable, so the effect
// never re-ran). A response lost across a disconnect left both
// surfaces stale for the whole session. They now take the socket
// layer's reconnectCount signal — the Speech/Usage panels'
// established pattern — and refetch when it changes.
function makeWsRef() {
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
    },
  };
  return wsRef;
}

const sentTypes = (wsRef) =>
  wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw).type);

describe('Dashboard / Projects refetch on reconnect (D2-09)', () => {
  test('Dashboard re-requests memory stats when reconnectCount changes', () => {
    const wsRef = makeWsRef();
    const { rerender } = render(
      <Dashboard status="Online" wsRef={wsRef} reconnectCount={0} />,
    );
    expect(sentTypes(wsRef).filter((t) => t === 'MEMORY_EPISODES_REQUEST')).toHaveLength(1);
    rerender(<Dashboard status="Online" wsRef={wsRef} reconnectCount={1} />);
    expect(sentTypes(wsRef).filter((t) => t === 'MEMORY_EPISODES_REQUEST')).toHaveLength(2);
  });

  test('Projects re-requests the workspace list when reconnectCount changes', () => {
    const wsRef = makeWsRef();
    const { rerender } = render(<Projects wsRef={wsRef} reconnectCount={0} />);
    expect(sentTypes(wsRef).filter((t) => t === 'WORKSPACE_LIST_REQUEST')).toHaveLength(1);
    rerender(<Projects wsRef={wsRef} reconnectCount={1} />);
    expect(sentTypes(wsRef).filter((t) => t === 'WORKSPACE_LIST_REQUEST')).toHaveLength(2);
  });
});
