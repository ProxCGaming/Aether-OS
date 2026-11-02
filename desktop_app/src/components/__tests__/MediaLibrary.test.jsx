import React, { act } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

import MediaLibrary from '../MediaLibrary';

// Stub scrollIntoView for jsdom
Element.prototype.scrollIntoView = jest.fn();

describe('MediaLibrary Component', () => {
  const sampleSessions = [
    { id: 'sess-1', title: 'UX Research' },
    { id: 'sess-2', title: 'Architecture Review' },
  ];

  const sampleMedia = [
    {
      id: 'med-1',
      session_id: 'sess-1',
      message_id: 'msg-1',
      collection: 'user',
      media_type: 'image',
      file_path: '/home/user/screenshots/snip_login.png',
      file_size: 45000,
      mime_type: 'image/png',
      status: 'pending',
      probe_metadata: { origin: 'snip' },
      origin: 'snip',
      tags: ['login', 'ui'],
      user_notes: 'Login screen capture',
      created_at: 1000,
      file_exists: true,
    },
    {
      id: 'med-2',
      session_id: 'sess-1',
      message_id: 'msg-2',
      collection: 'user',
      media_type: 'video',
      file_path: '/home/user/videos/demo_walkthrough.mp4',
      file_size: 1200000,
      mime_type: 'video/mp4',
      status: 'analyzed',
      probe_metadata: { origin: 'user_upload' },
      origin: 'user_upload',
      tags: ['demo', 'presentation'],
      user_notes: 'User testing video',
      created_at: 2000,
      file_exists: true,
    },
    {
      id: 'med-3',
      session_id: 'sess-2',
      message_id: 'msg-3',
      collection: 'generated',
      media_type: 'image',
      file_path: '/home/user/workspace/diagram.png',
      file_size: 85000,
      mime_type: 'image/png',
      status: 'analyzed',
      probe_metadata: { origin: 'agent_generated', tool: 'write_file' },
      origin: 'agent_generated',
      tags: ['diagram', 'database'],
      user_notes: 'Generated DB schema',
      created_at: 3000,
      file_exists: true,
    },
    {
      id: 'med-4',
      session_id: 'sess-2',
      message_id: 'msg-4',
      collection: 'user',
      media_type: 'image',
      file_path: '/home/user/workspace/moved_file.png',
      file_size: 30000,
      mime_type: 'image/png',
      status: 'pending',
      probe_metadata: { origin: 'user_upload' },
      origin: 'user_upload',
      tags: ['lost'],
      user_notes: 'This file was moved',
      created_at: 4000,
      file_exists: false, // File moved externally!
    },
  ];

  const mockWsRef = {
    current: {
      readyState: 1, // OPEN
      send: jest.fn(),
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
    },
  };

  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('renders media grid with origin badges and metadata', () => {
    render(
      <MediaLibrary
        initialMedia={sampleMedia}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    expect(screen.getByTestId('media-library-view')).toBeInTheDocument();
    expect(screen.getByTestId('media-count-badge')).toHaveTextContent('4');

    // Cards render
    expect(screen.getByTestId('media-card-med-1')).toBeInTheDocument();
    expect(screen.getByTestId('media-card-med-2')).toBeInTheDocument();
    expect(screen.getByTestId('media-card-med-3')).toBeInTheDocument();
    expect(screen.getByTestId('media-card-med-4')).toBeInTheDocument();

    // Origin badges
    expect(screen.getByTestId('media-origin-med-1')).toHaveTextContent(/snip/i);
    expect(screen.getByTestId('media-origin-med-2')).toHaveTextContent(/upload/i);
    expect(screen.getByTestId('media-origin-med-3')).toHaveTextContent(/generated/i);

    // Session resolution
    expect(screen.getByTestId('media-session-med-1')).toHaveTextContent('UX Research');
    expect(screen.getByTestId('media-session-med-3')).toHaveTextContent('Architecture Review');
  });

  test('gracefully degrades to missing file card when file does not exist on disk', () => {
    render(
      <MediaLibrary
        initialMedia={sampleMedia}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    // med-4 has file_exists: false
    expect(screen.getByTestId('media-missing-badge-med-4')).toBeInTheDocument();
    expect(screen.getByTestId('media-missing-badge-med-4')).toHaveTextContent(/missing file/i);

    // Existing files should not have missing badge
    expect(screen.queryByTestId('media-missing-badge-med-1')).not.toBeInTheDocument();
  });

  test('filters media by collection tabs (All, User, Snips, Generated)', () => {
    render(
      <MediaLibrary
        initialMedia={sampleMedia}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    // Switch to Generated
    const genTab = screen.getByTestId('collection-tab-generated');
    fireEvent.click(genTab);
    expect(screen.getByTestId('media-card-med-3')).toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-2')).not.toBeInTheDocument();

    // Switch to Snips
    const snipsTab = screen.getByTestId('collection-tab-snips');
    fireEvent.click(snipsTab);
    expect(screen.getByTestId('media-card-med-1')).toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-2')).not.toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-3')).not.toBeInTheDocument();

    // Switch to User
    const userTab = screen.getByTestId('collection-tab-user');
    fireEvent.click(userTab);
    expect(screen.getByTestId('media-card-med-1')).toBeInTheDocument();
    expect(screen.getByTestId('media-card-med-2')).toBeInTheDocument();
    expect(screen.getByTestId('media-card-med-4')).toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-3')).not.toBeInTheDocument();

    // Switch back to All
    const allTab = screen.getByTestId('collection-tab-all');
    fireEvent.click(allTab);
    expect(screen.getByTestId('media-card-med-1')).toBeInTheDocument();
    expect(screen.getByTestId('media-card-med-2')).toBeInTheDocument();
    expect(screen.getByTestId('media-card-med-3')).toBeInTheDocument();
    expect(screen.getByTestId('media-card-med-4')).toBeInTheDocument();
  });

  test('filters media items by search query', () => {
    render(
      <MediaLibrary
        initialMedia={sampleMedia}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    const searchInput = screen.getByTestId('media-search-input');

    // Search by tag
    fireEvent.change(searchInput, { target: { value: 'database' } });
    expect(screen.getByTestId('media-card-med-3')).toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-1')).not.toBeInTheDocument();

    // Search by note
    fireEvent.change(searchInput, { target: { value: 'testing' } });
    expect(screen.getByTestId('media-card-med-2')).toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-1')).not.toBeInTheDocument();

    // Search by filename
    fireEvent.change(searchInput, { target: { value: 'snip_login' } });
    expect(screen.getByTestId('media-card-med-1')).toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-2')).not.toBeInTheDocument();
  });

  test('clicking Jump to conversation navigates to session and targets message', () => {
    const onNavigate = jest.fn();
    render(
      <MediaLibrary
        initialMedia={sampleMedia}
        sessions={sampleSessions}
        onNavigateToSession={onNavigate}
      />
    );

    const jumpBtn = screen.getByTestId('jump-to-conversation-med-2');
    fireEvent.click(jumpBtn);

    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onNavigate).toHaveBeenCalledWith(expect.objectContaining({
      session_id: 'sess-1',
      message_id: 'msg-2',
      id: 'med-2',
    }));
  });

  test('opening detail modal, editing tags and notes, and saving', async () => {
    render(
      <MediaLibrary
        initialMedia={sampleMedia}
        sessions={sampleSessions}
        wsRef={mockWsRef}
        onNavigateToSession={jest.fn()}
      />
    );

    // Open detail modal for med-1
    const card = screen.getByTestId('media-card-med-1');
    fireEvent.click(card);

    expect(screen.getByTestId('media-detail-modal')).toBeInTheDocument();
    expect(screen.getByTestId('media-notes-textarea')).toHaveValue('Login screen capture');

    // Add a new tag
    const tagInput = screen.getByTestId('media-tag-input');
    fireEvent.change(tagInput, { target: { value: 'frontend' } });
    const addTagBtn = screen.getByTestId('media-add-tag-btn');
    fireEvent.click(addTagBtn);

    // Update notes
    const notesInput = screen.getByTestId('media-notes-textarea');
    fireEvent.change(notesInput, { target: { value: 'Updated notes for login capture' } });

    // Save changes
    const saveBtn = screen.getByTestId('media-save-metadata-btn');
    fireEvent.click(saveBtn);

    expect(mockWsRef.current.send).toHaveBeenCalledWith(
      expect.stringContaining('MEDIA_METADATA_UPDATE_REQUEST')
    );
    const sentData = JSON.parse(mockWsRef.current.send.mock.calls[0][0]);
    expect(sentData.payload).toMatchObject({
      media_id: 'med-1',
      tags: expect.arrayContaining(['login', 'ui', 'frontend']),
      notes: 'Updated notes for login capture',
    });
  });

  test('deleting a media entry sends MEDIA_DELETE_REQUEST and removes card', () => {
    render(
      <MediaLibrary
        initialMedia={sampleMedia}
        sessions={sampleSessions}
        wsRef={mockWsRef}
        onNavigateToSession={jest.fn()}
      />
    );

    // Click delete on med-1
    const deleteBtn = screen.getByTestId('media-delete-btn-med-1');
    fireEvent.click(deleteBtn);

    // Confirm modal appears
    expect(screen.getByTestId('delete-confirm-modal')).toBeInTheDocument();

    // Toggle delete_underlying_file checkbox
    const fileCheckbox = screen.getByTestId('delete-file-checkbox');
    expect(fileCheckbox).not.toBeChecked(); // defaults to false (index only)
    fireEvent.click(fileCheckbox);
    expect(fileCheckbox).toBeChecked();

    // Confirm deletion
    const confirmDeleteBtn = screen.getByTestId('confirm-delete-action-btn');
    fireEvent.click(confirmDeleteBtn);

    expect(mockWsRef.current.send).toHaveBeenCalledWith(
      expect.stringContaining('MEDIA_DELETE_REQUEST')
    );
    const sentData = JSON.parse(mockWsRef.current.send.mock.calls[0][0]);
    expect(sentData.payload).toMatchObject({
      media_id: 'med-1',
      delete_underlying_file: true,
    });
  });

  test('real-time events update media list', () => {
    let wsMessageHandler = null;
    mockWsRef.current.addEventListener.mockImplementation((event, handler) => {
      if (event === 'message') {
        wsMessageHandler = handler;
      }
    });

    render(
      <MediaLibrary
        initialMedia={sampleMedia}
        sessions={sampleSessions}
        wsRef={mockWsRef}
        onNavigateToSession={jest.fn()}
      />
    );

    expect(screen.getByTestId('media-count-badge')).toHaveTextContent('4');

    // Simulate MEDIA_RECORDED
    act(() => {
      wsMessageHandler({
        data: JSON.stringify({
          type: 'MEDIA_RECORDED',
          payload: {
            id: 'med-new',
            session_id: 'sess-1',
            message_id: 'msg-new',
            collection: 'user',
            media_type: 'image',
            file_path: '/path/new_snip.png',
            file_size: 1234,
            mime_type: 'image/png',
            probe_metadata: { origin: 'snip' },
            origin: 'snip',
            tags: ['new'],
            user_notes: '',
            created_at: 5000,
            file_exists: true,
          },
        }),
      });
    });

    expect(screen.getByTestId('media-count-badge')).toHaveTextContent('5');
    expect(screen.getByTestId('media-card-med-new')).toBeInTheDocument();

    // Simulate MEDIA_DELETE_RESPONSE
    act(() => {
      wsMessageHandler({
        data: JSON.stringify({
          type: 'MEDIA_DELETE_RESPONSE',
          payload: {
            media_id: 'med-new',
            deleted: true,
            file_deleted: false,
          },
        }),
      });
    });

    expect(screen.getByTestId('media-count-badge')).toHaveTextContent('4');
    expect(screen.queryByTestId('media-card-med-new')).not.toBeInTheDocument();
  });
});


// ---------------------------------------------------------------------------
// N07 (Epic 8 Astra review): analysis state and backend recall must
// reach the Library UI. Pre-fix the component registered no handler
// for MEDIA_ANALYSIS_PROGRESS / MEDIA_ANALYZED and search was a local
// metadata filter only, so transcript/semantic matches never surfaced.
// ---------------------------------------------------------------------------
describe('MediaLibrary N07 — analysis events + backend recall search', () => {
  const sessions = [{ id: 'sess-1', title: 'UX Research' }];

  function makeWs() {
    const handlers = [];
    const ws = {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => {
        if (event === 'message') handlers.push(handler);
      }),
      removeEventListener: jest.fn(),
    };
    const deliver = (msg) => {
      act(() => {
        handlers[handlers.length - 1]({ data: JSON.stringify(msg) });
      });
    };
    return { ws, deliver };
  }

  test('analysis events update the library item live', () => {
    const { ws, deliver } = makeWs();
    const pending = {
      id: 'med-9',
      session_id: 'sess-1',
      message_id: 'msg-9',
      collection: 'user',
      media_type: 'image',
      file_path: '/home/user/photos/bicycle.png',
      file_size: 12000,
      mime_type: 'image/png',
      status: 'pending',
      probe_metadata: { origin: 'user_upload' },
      origin: 'user_upload',
      tags: [],
      user_notes: '',
      created_at: 5000,
      file_exists: true,
    };
    render(
      <MediaLibrary
        initialMedia={[pending]}
        sessions={sessions}
        wsRef={{ current: ws }}
        onNavigateToSession={jest.fn()}
      />
    );
    expect(screen.queryByText('#bicycle')).not.toBeInTheDocument();

    deliver({
      type: 'MEDIA_ANALYSIS_PROGRESS',
      payload: { media_id: 'med-9', status: 'analyzing' },
    });
    deliver({
      type: 'MEDIA_ANALYZED',
      payload: {
        ...pending,
        status: 'analyzed',
        summary_text: 'A red bicycle parked by a wall',
        tags: ['bicycle', 'wall'],
      },
    });

    // The analyzed tags/summary are now part of the rendered item.
    expect(screen.getByText('#bicycle')).toBeInTheDocument();
  });

  test('search is served by backend recall — a transcript-only match surfaces', () => {
    jest.useFakeTimers();
    try {
      const { ws, deliver } = makeWs();
      const local = {
        id: 'med-1',
        session_id: 'sess-1',
        message_id: 'msg-1',
        collection: 'user',
        media_type: 'video',
        file_path: '/home/user/videos/walkthrough.mp4',
        file_size: 900000,
        mime_type: 'video/mp4',
        status: 'analyzed',
        probe_metadata: { origin: 'user_upload' },
        origin: 'user_upload',
        tags: ['demo'],
        user_notes: '',
        summary_text: 'A dashboard tour',
        created_at: 1000,
        file_exists: true,
      };
      render(
        <MediaLibrary
          initialMedia={[local]}
          sessions={sessions}
          wsRef={{ current: ws }}
          onNavigateToSession={jest.fn()}
        />
      );

      // "quarterly earnings" appears in NO local metadata field — only
      // in the transcript of an item the local list does not hold.
      fireEvent.change(screen.getByTestId('media-search-input'), {
        target: { value: 'quarterly earnings' },
      });
      act(() => {
        jest.advanceTimersByTime(500);
      });

      const searchSends = ws.send.mock.calls
        .map(([raw]) => {
          try { return JSON.parse(raw); } catch { return null; }
        })
        .filter((m) => m && m.type === 'MEDIA_SEARCH_REQUEST');
      expect(searchSends.length).toBeGreaterThan(0);
      expect(searchSends[searchSends.length - 1].payload.query).toBe('quarterly earnings');

      deliver({
        type: 'MEDIA_SEARCH_RESPONSE',
        payload: {
          query: 'quarterly earnings',
          degraded: false,
          media: [{
            id: 'med-t1',
            session_id: 'sess-1',
            message_id: 'msg-t1',
            collection: 'user',
            media_type: 'video',
            file_path: '/home/user/videos/earnings_call.mp4',
            file_size: 2400000,
            mime_type: 'video/mp4',
            status: 'analyzed',
            probe_metadata: { origin: 'user_upload' },
            origin: 'user_upload',
            tags: ['call'],
            user_notes: '',
            summary_text: 'A recorded call',
            transcript_text: 'the quarterly earnings call numbers look strong',
            transcript_status: 'done',
            created_at: 6000,
            file_exists: true,
          }],
        },
      });

      expect(screen.getByTestId('media-card-med-t1')).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});


// ---------------------------------------------------------------------------
// OP12 (Epic 8 PRD Open Point 12): deleting a library entry is
// index-only by default; "also delete the file" is an explicit
// per-delete choice. The engine answers with file_deleted — when the
// user asked for the file to go and it did NOT, the UI must say so
// honestly instead of implying both are gone; when the engine says
// the entry itself was not deleted, the card must come back.
// ---------------------------------------------------------------------------
describe('MediaLibrary OP12 — also-delete-file option + honest outcome', () => {
  const sessions = [{ id: 'sess-1', title: 'UX Research' }];

  function makeWs() {
    const handlers = [];
    const ws = {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => {
        if (event === 'message') handlers.push(handler);
      }),
      removeEventListener: jest.fn(),
    };
    const deliver = (msg) => {
      act(() => {
        handlers[handlers.length - 1]({ data: JSON.stringify(msg) });
      });
    };
    return { ws, deliver };
  }

  const item = {
    id: 'med-1',
    session_id: 'sess-1',
    message_id: 'msg-1',
    collection: 'user',
    media_type: 'image',
    file_path: '/home/user/photos/keepme.png',
    file_size: 12000,
    mime_type: 'image/png',
    status: 'analyzed',
    probe_metadata: { origin: 'user_upload' },
    origin: 'user_upload',
    tags: [],
    user_notes: '',
    created_at: 5000,
    file_exists: true,
  };

  function renderLibrary(ws) {
    return render(
      <MediaLibrary
        initialMedia={[item]}
        sessions={sessions}
        wsRef={{ current: ws }}
        onNavigateToSession={jest.fn()}
      />
    );
  }

  function openDeleteModal() {
    fireEvent.click(screen.getByTestId('media-delete-btn-med-1'));
    expect(screen.getByTestId('delete-confirm-modal')).toBeInTheDocument();
  }

  function lastSentPayload(ws) {
    const calls = ws.send.mock.calls;
    return JSON.parse(calls[calls.length - 1][0]).payload;
  }

  test('unchecked checkbox sends an index-only delete', () => {
    const { ws } = makeWs();
    renderLibrary(ws);

    openDeleteModal();
    const checkbox = screen.getByTestId('delete-file-checkbox');
    expect(checkbox).not.toBeChecked();

    fireEvent.click(screen.getByTestId('confirm-delete-action-btn'));

    expect(lastSentPayload(ws)).toMatchObject({
      media_id: 'med-1',
      delete_underlying_file: false,
    });
  });

  test('file-delete failure is surfaced honestly, entry still removed', () => {
    const { ws, deliver } = makeWs();
    renderLibrary(ws);

    openDeleteModal();
    fireEvent.click(screen.getByTestId('delete-file-checkbox'));
    fireEvent.click(screen.getByTestId('confirm-delete-action-btn'));
    expect(lastSentPayload(ws)).toMatchObject({ delete_underlying_file: true });

    deliver({
      type: 'MEDIA_DELETE_RESPONSE',
      payload: { media_id: 'med-1', deleted: true, file_deleted: false },
    });

    const notice = screen.getByTestId('media-delete-notice');
    expect(notice).toHaveTextContent(/could not be deleted from disk/i);
    expect(notice).toHaveTextContent(/keepme\.png/);
    // The entry itself is gone — the notice must not resurrect it.
    expect(screen.queryByTestId('media-card-med-1')).not.toBeInTheDocument();
  });

  test('confirmed file deletion shows no failure notice', () => {
    const { ws, deliver } = makeWs();
    renderLibrary(ws);

    openDeleteModal();
    fireEvent.click(screen.getByTestId('delete-file-checkbox'));
    fireEvent.click(screen.getByTestId('confirm-delete-action-btn'));

    deliver({
      type: 'MEDIA_DELETE_RESPONSE',
      payload: { media_id: 'med-1', deleted: true, file_deleted: true },
    });

    expect(screen.queryByTestId('media-delete-notice')).not.toBeInTheDocument();
    expect(screen.queryByTestId('media-card-med-1')).not.toBeInTheDocument();
  });

  test('engine refusing the entry delete restores the card and says so', () => {
    const { ws, deliver } = makeWs();
    renderLibrary(ws);

    openDeleteModal();
    fireEvent.click(screen.getByTestId('confirm-delete-action-btn'));
    // Optimistic removal happened on confirm.
    expect(screen.queryByTestId('media-card-med-1')).not.toBeInTheDocument();

    deliver({
      type: 'MEDIA_DELETE_RESPONSE',
      payload: { media_id: 'med-1', deleted: false, file_deleted: false },
    });

    expect(screen.getByTestId('media-delete-notice')).toHaveTextContent(/still there/i);
    expect(screen.getByTestId('media-card-med-1')).toBeInTheDocument();
  });
});
