import React, { act } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import MediaLibrary from '../MediaLibrary';
import GlobalArtifactsView from '../GlobalArtifactsView';

// Wave 3 (D3 chunk) — the two library surfaces:
//   D3-02 — MediaLibrary: selecting an item must not re-subscribe
//           the socket listener or re-fetch the whole list.
//   D3-03 — both: a list request the engine never answers must
//           resolve to an error state, not "loading" forever.
//   D3-04 — both: library inputs keep a keyboard focus ring (the
//           .aether-search-input class), not an inline outline:none.
//   D3-07 — MediaLibrary renders the media itself (file:// seam via
//           toFileUrl), in the grid and in the detail modal.
//   D3-21 — GlobalArtifactsView: every counted kind is selectable.
//   D3-24 — MediaLibrary formatFileSize works at >= 1 TB.
//   D3-25 — MediaLibrary never fabricates an origin label.

function makeWsRef() {
  let handler = null;
  const calls = { add: 0, remove: 0 };
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') { handler = cb; calls.add += 1; }
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') { calls.remove += 1; if (handler === cb) handler = null; }
      }),
    },
  };
  return {
    wsRef,
    calls,
    deliver: (data) => act(() => { if (handler) handler({ data: JSON.stringify(data) }); }),
    sentTypes: () => wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw).type),
  };
}

const mediaItem = {
  id: 'med-1', session_id: 'sess-1', message_id: 'msg-1', collection: 'user',
  media_type: 'image', file_path: '/home/user/shots/pic.png', file_size: 45000,
  mime_type: 'image/png', status: 'analyzed', origin: 'user_upload',
  tags: [], user_notes: '', created_at: 1000, file_exists: true,
};

describe('D3-02 — selecting media does not re-subscribe or re-fetch', () => {
  it('keeps one listener and one list request across open/close of the detail modal', () => {
    const { wsRef, calls, deliver, sentTypes } = makeWsRef();
    render(<MediaLibrary wsRef={wsRef} sessions={[]} />);
    deliver({ type: 'MEDIA_LIST_RESPONSE', payload: { media: [mediaItem] } });
    expect(sentTypes().filter((t) => t === 'MEDIA_LIST_REQUEST')).toHaveLength(1);
    expect(calls.add).toBe(1);

    fireEvent.click(screen.getByTestId('media-card-med-1'));
    expect(screen.getByTestId('media-detail-modal')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('media-modal-close-btn'));
    expect(screen.queryByTestId('media-detail-modal')).not.toBeInTheDocument();

    expect(sentTypes().filter((t) => t === 'MEDIA_LIST_REQUEST')).toHaveLength(1);
    expect(calls.add).toBe(1);
    expect(calls.remove).toBe(0);
  });
});

describe('D3-03 — unanswered list fetches resolve to an error state', () => {
  beforeEach(() => { jest.useFakeTimers(); });
  afterEach(() => { jest.useRealTimers(); });

  it('MediaLibrary: no MEDIA_LIST_RESPONSE within the window -> error row', () => {
    const { wsRef } = makeWsRef();
    render(<MediaLibrary wsRef={wsRef} sessions={[]} />);
    expect(screen.getByTestId('media-list-load-state').dataset.loadDisplay).toBe('loading');
    act(() => { jest.advanceTimersByTime(35000); });
    expect(screen.getByTestId('media-list-load-state').dataset.loadDisplay).toBe('error');
  });

  it('GlobalArtifactsView: no ARTIFACT_LIST_RESPONSE within the window -> error row', () => {
    const { wsRef } = makeWsRef();
    render(<GlobalArtifactsView wsRef={wsRef} sessions={[]} />);
    expect(screen.getByTestId('artifacts-list-load-state').dataset.loadDisplay).toBe('loading');
    act(() => { jest.advanceTimersByTime(35000); });
    expect(screen.getByTestId('artifacts-list-load-state').dataset.loadDisplay).toBe('error');
  });

  it('GlobalArtifactsView: an answer before the deadline keeps the ready state', () => {
    const { wsRef, deliver } = makeWsRef();
    render(<GlobalArtifactsView wsRef={wsRef} sessions={[]} />);
    act(() => { jest.advanceTimersByTime(1000); });
    deliver({
      type: 'ARTIFACT_LIST_RESPONSE',
      request_id: 'global_artifacts_9',
      payload: { global: true, artifacts: [] },
    });
    act(() => { jest.advanceTimersByTime(60000); });
    expect(screen.getByTestId('empty-artifacts-state')).toBeInTheDocument();
    expect(screen.queryByTestId('artifacts-list-load-state')).not.toBeInTheDocument();
  });
});

describe('D3-04 — library inputs carry the shared focus-ring class', () => {
  it('GlobalArtifactsView search input', () => {
    render(<GlobalArtifactsView initialArtifacts={[]} sessions={[]} />);
    const input = screen.getByTestId('artifacts-search-input');
    expect(input).toHaveClass('aether-search-input');
    expect(input.style.outline).not.toBe('none');
  });

  it('MediaLibrary search, tag and notes inputs', () => {
    render(<MediaLibrary initialMedia={[mediaItem]} sessions={[]} />);
    const search = screen.getByTestId('media-search-input');
    expect(search).toHaveClass('aether-search-input');
    expect(search.style.outline).not.toBe('none');

    fireEvent.click(screen.getByTestId('media-card-med-1'));
    const tagInput = screen.getByTestId('media-tag-input');
    expect(tagInput).toHaveClass('aether-search-input');
    expect(tagInput.style.outline).not.toBe('none');
    const notes = screen.getByTestId('media-notes-textarea');
    expect(notes).toHaveClass('aether-search-input');
    expect(notes.style.outline).not.toBe('none');
  });
});

describe('D3-07 — MediaLibrary renders the media itself', () => {
  const videoItem = {
    ...mediaItem, id: 'med-2', media_type: 'video',
    file_path: '/home/user/videos/clip.mp4', mime_type: 'video/mp4',
  };

  it('renders an <img> for image items and a <video> for video items in the grid', () => {
    render(<MediaLibrary initialMedia={[mediaItem, videoItem]} sessions={[]} />);
    const img = screen.getByTestId('media-thumb-med-1');
    expect(img.tagName).toBe('IMG');
    expect(img.getAttribute('src')).toBe('file:///home/user/shots/pic.png');
    const video = screen.getByTestId('media-thumb-med-2');
    expect(video.tagName).toBe('VIDEO');
    expect(video.getAttribute('src')).toBe('file:///home/user/videos/clip.mp4');
  });

  it('renders the media in the detail modal', () => {
    render(<MediaLibrary initialMedia={[mediaItem]} sessions={[]} />);
    fireEvent.click(screen.getByTestId('media-card-med-1'));
    const preview = screen.getByTestId('media-detail-preview');
    expect(preview.tagName).toBe('IMG');
    expect(preview.getAttribute('src')).toBe('file:///home/user/shots/pic.png');
  });

  it('keeps the missing-file badge (and no broken img) for files gone from disk', () => {
    const missing = { ...mediaItem, id: 'med-9', file_exists: false };
    render(<MediaLibrary initialMedia={[missing]} sessions={[]} />);
    expect(screen.getByTestId('media-missing-badge-med-9')).toBeInTheDocument();
    expect(screen.queryByTestId('media-thumb-med-9')).not.toBeInTheDocument();
  });
});

describe('D3-24 / D3-25 — honest sizes and origin labels', () => {
  it('formats a >= 1 TB file size with a real unit', () => {
    const huge = { ...mediaItem, id: 'med-big', file_size: Math.floor(1.5 * 1024 ** 4) };
    render(<MediaLibrary initialMedia={[huge]} sessions={[]} />);
    expect(screen.getByText('1.5 TB')).toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
  });

  it('labels an unrecognised origin as Unknown, never a fabricated "User upload"', () => {
    const odd = { ...mediaItem, id: 'med-odd', origin: 'carrier_pigeon', collection: 'user' };
    render(<MediaLibrary initialMedia={[odd]} sessions={[]} />);
    expect(screen.getByTestId('media-origin-med-odd')).toHaveTextContent('Unknown');
  });

  it('still labels a generated-collection item as Agent generated', () => {
    const gen = { ...mediaItem, id: 'med-gen', origin: '', collection: 'generated' };
    render(<MediaLibrary initialMedia={[gen]} sessions={[]} />);
    expect(screen.getByTestId('media-origin-med-gen')).toHaveTextContent(/agent generated/i);
  });
});

describe('D3-21 — GlobalArtifactsView kind filter covers every counted kind', () => {
  const artifacts = [
    {
      id: 'a1', session_id: 's1', task_id: 't1', kind: 'file',
      title: 'code.py', file_path: '/x/code.py', created_at: 1,
    },
    {
      id: 'a2', session_id: 's1', task_id: 't1', kind: 'html',
      title: 'page.html', file_path: '/x/page.html', created_at: 2,
    },
  ];

  it('offers a chip for the html kind and filters by it', () => {
    render(<GlobalArtifactsView initialArtifacts={artifacts} sessions={[]} />);
    const chip = screen.getByTestId('filter-kind-html');
    expect(chip).toHaveTextContent('1');
    fireEvent.click(chip);
    expect(screen.getByTestId('artifact-item-a2')).toBeInTheDocument();
    expect(screen.queryByTestId('artifact-item-a1')).not.toBeInTheDocument();
  });
});
