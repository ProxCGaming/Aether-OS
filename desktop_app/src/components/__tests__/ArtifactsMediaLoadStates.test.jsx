import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import GlobalArtifactsView from '../GlobalArtifactsView';
import MediaLibrary from '../MediaLibrary';

// RUX-C-09 (Wave 3 slice 2): the artifacts and media list surfaces
// rendered their designed empty states from the first frame —
// during the list fetch and through any outage. These tests pin
// the idiom: pre-answer shows loading/offline, an answered-empty
// list shows the genuine empty state, and a failed list reply is
// an error state that never masquerades as "nothing here".

function makeWsRef() {
  let handler = null;
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => { if (event === 'message') handler = cb; }),
      removeEventListener: jest.fn(),
    },
  };
  return { wsRef, deliver: (data) => act(() => { handler({ data: JSON.stringify(data) }); }) };
}

describe('GlobalArtifactsView list states (RUX-C-09)', () => {
  it('pre-answer shows loading, not "No artifacts found"', () => {
    const { wsRef } = makeWsRef();
    render(<GlobalArtifactsView wsRef={wsRef} sessions={[]} />);
    const row = screen.getByTestId('artifacts-list-load-state');
    expect(row.dataset.loadDisplay).toBe('loading');
    expect(screen.queryByTestId('empty-artifacts-state')).not.toBeInTheDocument();
  });

  it('pre-answer while disconnected shows the waiting-for-engine state', () => {
    const { wsRef } = makeWsRef();
    render(<GlobalArtifactsView wsRef={wsRef} sessions={[]} isConnected={false} />);
    const row = screen.getByTestId('artifacts-list-load-state');
    expect(row.dataset.loadDisplay).toBe('offline');
    expect(row.textContent).toMatch(/Waiting for the engine/);
  });

  it('an answered-empty global list renders the genuine empty state', () => {
    const { wsRef, deliver } = makeWsRef();
    render(<GlobalArtifactsView wsRef={wsRef} sessions={[]} />);
    deliver({
      type: 'ARTIFACT_LIST_RESPONSE',
      request_id: 'global_artifacts_1',
      payload: { global: true, artifacts: [] },
    });
    expect(screen.queryByTestId('artifacts-list-load-state')).not.toBeInTheDocument();
    expect(screen.getByTestId('empty-artifacts-state')).toBeInTheDocument();
  });

  it('a list reply carrying an error is an error state, not an empty library', () => {
    const { wsRef, deliver } = makeWsRef();
    render(<GlobalArtifactsView wsRef={wsRef} sessions={[]} />);
    deliver({
      type: 'ARTIFACT_LIST_RESPONSE',
      request_id: 'global_artifacts_2',
      payload: { global: true, error: 'artifact store unavailable' },
    });
    const row = screen.getByTestId('artifacts-list-load-state');
    expect(row.dataset.loadDisplay).toBe('error');
    expect(screen.queryByTestId('empty-artifacts-state')).not.toBeInTheDocument();
  });
});

describe('MediaLibrary list states (RUX-C-09)', () => {
  it('pre-answer shows loading, not "No media items yet"', () => {
    const { wsRef } = makeWsRef();
    render(<MediaLibrary wsRef={wsRef} sessions={[]} />);
    const row = screen.getByTestId('media-list-load-state');
    expect(row.dataset.loadDisplay).toBe('loading');
    expect(screen.queryByTestId('empty-media-state')).not.toBeInTheDocument();
  });

  it('pre-answer while disconnected shows the waiting-for-engine state', () => {
    const { wsRef } = makeWsRef();
    render(<MediaLibrary wsRef={wsRef} sessions={[]} isConnected={false} />);
    const row = screen.getByTestId('media-list-load-state');
    expect(row.dataset.loadDisplay).toBe('offline');
    expect(row.textContent).toMatch(/Waiting for the engine/);
  });

  it('an answered-empty list renders the genuine empty state', () => {
    const { wsRef, deliver } = makeWsRef();
    render(<MediaLibrary wsRef={wsRef} sessions={[]} />);
    deliver({ type: 'MEDIA_LIST_RESPONSE', payload: { media: [] } });
    expect(screen.queryByTestId('media-list-load-state')).not.toBeInTheDocument();
    expect(screen.getByTestId('empty-media-state')).toBeInTheDocument();
  });

  it('a list reply carrying an error is an error state, not an empty library', () => {
    const { wsRef, deliver } = makeWsRef();
    render(<MediaLibrary wsRef={wsRef} sessions={[]} />);
    deliver({ type: 'MEDIA_LIST_RESPONSE', payload: { error: 'media store unavailable' } });
    const row = screen.getByTestId('media-list-load-state');
    expect(row.dataset.loadDisplay).toBe('error');
    expect(screen.queryByTestId('empty-media-state')).not.toBeInTheDocument();
  });
});
