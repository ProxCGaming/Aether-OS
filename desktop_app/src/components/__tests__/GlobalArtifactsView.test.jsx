import React, { act } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

import GlobalArtifactsView from '../GlobalArtifactsView';

describe('GlobalArtifactsView Component', () => {
  const sampleSessions = [
    { id: 'sess-1', title: 'Backend Dev' },
    { id: 'sess-2', title: 'Frontend UI' },
    { id: 'sess-3', title: 'Data Pipeline' },
  ];

  const sampleArtifacts = [
    {
      id: 'art-1',
      session_id: 'sess-1',
      task_id: 'task-auth',
      kind: 'file',
      title: 'auth_service.py',
      file_path: '/src/auth_service.py',
      created_at: 1000,
    },
    {
      id: 'art-2',
      session_id: 'sess-1',
      task_id: 'task-auth',
      kind: 'diff',
      title: 'auth_service.py',
      file_path: '/src/auth_service.py',
      content: '--- a\n+++ b',
      created_at: 1050,
    },
    {
      id: 'art-3',
      session_id: 'sess-2',
      task_id: 'task-ui',
      kind: 'file',
      title: 'Header.jsx',
      file_path: '/src/components/Header.jsx',
      created_at: 2000,
    },
    {
      id: 'art-4',
      session_id: 'sess-3',
      task_id: 'task-data',
      kind: 'report',
      title: 'benchmark_report.md',
      file_path: '/reports/benchmark_report.md',
      created_at: 3000,
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

  test('renders cross-session artifacts with correct session and task attribution', () => {
    render(
      <GlobalArtifactsView
        artifacts={sampleArtifacts}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    expect(screen.getByTestId('global-artifacts-view')).toBeInTheDocument();
    expect(screen.getByTestId('artifacts-count-badge')).toHaveTextContent('4');

    // Check all titles appear
    expect(screen.getByText('auth_service.py', { selector: '[data-testid="artifact-title-art-1"]' })).toBeInTheDocument();
    expect(screen.getByText('Header.jsx')).toBeInTheDocument();
    expect(screen.getByText('benchmark_report.md')).toBeInTheDocument();

    // Check session attribution (resolves session title from sessions list)
    expect(screen.getByTestId('artifact-session-art-1')).toHaveTextContent('Backend Dev');
    expect(screen.getByTestId('artifact-session-art-3')).toHaveTextContent('Frontend UI');
    expect(screen.getByTestId('artifact-session-art-4')).toHaveTextContent('Data Pipeline');

    // Check task attribution
    expect(screen.getByTestId('artifact-task-art-1')).toHaveTextContent('task-auth');
    expect(screen.getByTestId('artifact-task-art-3')).toHaveTextContent('task-ui');
    expect(screen.getByTestId('artifact-task-art-4')).toHaveTextContent('task-data');
  });

  test('filters artifacts by search query (title, path, session name, task ID)', () => {
    render(
      <GlobalArtifactsView
        artifacts={sampleArtifacts}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    const searchInput = screen.getByTestId('artifacts-search-input');

    // Search by title
    fireEvent.change(searchInput, { target: { value: 'Header' } });
    expect(screen.getByText('Header.jsx')).toBeInTheDocument();
    expect(screen.queryByText('benchmark_report.md')).not.toBeInTheDocument();
    expect(screen.queryByTestId('artifact-title-art-1')).not.toBeInTheDocument();

    // Search by session name
    fireEvent.change(searchInput, { target: { value: 'Backend Dev' } });
    expect(screen.getByTestId('artifact-title-art-1')).toBeInTheDocument();
    expect(screen.queryByText('Header.jsx')).not.toBeInTheDocument();

    // Search by task ID
    fireEvent.change(searchInput, { target: { value: 'task-data' } });
    expect(screen.getByText('benchmark_report.md')).toBeInTheDocument();
    expect(screen.queryByText('Header.jsx')).not.toBeInTheDocument();

    // Search with no results
    fireEvent.change(searchInput, { target: { value: 'nonexistent-query-string' } });
    expect(screen.getByTestId('empty-search-state')).toBeInTheDocument();
    expect(screen.getByText(/No artifacts found/i)).toBeInTheDocument();
  });

  test('filters artifacts by kind tabs (All, Files, Diffs, Reports)', () => {
    render(
      <GlobalArtifactsView
        artifacts={sampleArtifacts}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    // Filter by Diffs
    const diffFilterBtn = screen.getByTestId('filter-kind-diff');
    fireEvent.click(diffFilterBtn);

    expect(screen.getByTestId('artifact-title-art-2')).toBeInTheDocument();
    expect(screen.queryByTestId('artifact-title-art-1')).not.toBeInTheDocument();
    expect(screen.queryByText('Header.jsx')).not.toBeInTheDocument();

    // Filter by Files
    const fileFilterBtn = screen.getByTestId('filter-kind-file');
    fireEvent.click(fileFilterBtn);

    expect(screen.getByTestId('artifact-title-art-1')).toBeInTheDocument();
    expect(screen.getByText('Header.jsx')).toBeInTheDocument();
    expect(screen.queryByTestId('artifact-title-art-2')).not.toBeInTheDocument();

    // Filter back to All
    const allFilterBtn = screen.getByTestId('filter-kind-all');
    fireEvent.click(allFilterBtn);

    expect(screen.getByTestId('artifact-title-art-1')).toBeInTheDocument();
    expect(screen.getByTestId('artifact-title-art-2')).toBeInTheDocument();
    expect(screen.getByText('Header.jsx')).toBeInTheDocument();
  });

  test('clicking an artifact invokes onNavigateToSession with the artifact data', () => {
    const onNavigate = jest.fn();
    render(
      <GlobalArtifactsView
        artifacts={sampleArtifacts}
        sessions={sampleSessions}
        onNavigateToSession={onNavigate}
      />
    );

    const artifactCard = screen.getByTestId('artifact-item-art-3');
    fireEvent.click(artifactCard);

    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onNavigate).toHaveBeenCalledWith(expect.objectContaining({
      id: 'art-3',
      session_id: 'sess-2',
      title: 'Header.jsx',
    }));
  });

  test('clicking Jump to Session button explicitly triggers navigation', () => {
    const onNavigate = jest.fn();
    render(
      <GlobalArtifactsView
        artifacts={sampleArtifacts}
        sessions={sampleSessions}
        onNavigateToSession={onNavigate}
      />
    );

    const jumpBtn = screen.getByTestId('jump-to-session-art-1');
    fireEvent.click(jumpBtn);

    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onNavigate).toHaveBeenCalledWith(expect.objectContaining({
      id: 'art-1',
      session_id: 'sess-1',
    }));
  });

  test('deleting a session cascades and removes only that session artifacts from view', () => {
    let wsMessageHandler = null;
    mockWsRef.current.addEventListener.mockImplementation((event, handler) => {
      if (event === 'message') {
        wsMessageHandler = handler;
      }
    });

    render(
      <GlobalArtifactsView
        initialArtifacts={sampleArtifacts}
        sessions={sampleSessions}
        wsRef={mockWsRef}
        onNavigateToSession={jest.fn()}
      />
    );

    expect(screen.getByTestId('artifacts-count-badge')).toHaveTextContent('4');

    // Simulate SESSION_DELETE_RESPONSE for sess-1
    act(() => {
      wsMessageHandler({
        data: JSON.stringify({
          type: 'SESSION_DELETE_RESPONSE',
          payload: { session_id: 'sess-1', deleted: true },
        }),
      });
    });

    // sess-1 artifacts (art-1, art-2) should be removed
    expect(screen.queryByTestId('artifact-title-art-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('artifact-title-art-2')).not.toBeInTheDocument();

    // sess-2 and sess-3 artifacts should remain intact
    expect(screen.getByText('Header.jsx')).toBeInTheDocument();
    expect(screen.getByText('benchmark_report.md')).toBeInTheDocument();
    expect(screen.getByTestId('artifacts-count-badge')).toHaveTextContent('2');
  });

  test('incoming ARTIFACT_RECORDED event appends new artifact to view', () => {
    let wsMessageHandler = null;
    mockWsRef.current.addEventListener.mockImplementation((event, handler) => {
      if (event === 'message') {
        wsMessageHandler = handler;
      }
    });

    render(
      <GlobalArtifactsView
        initialArtifacts={sampleArtifacts}
        sessions={sampleSessions}
        wsRef={mockWsRef}
        onNavigateToSession={jest.fn()}
      />
    );

    act(() => {
      wsMessageHandler({
        data: JSON.stringify({
          type: 'ARTIFACT_RECORDED',
          payload: {
            artifact_id: 'art-new',
            session_id: 'sess-2',
            task_id: 'task-ui-2',
            kind: 'file',
            title: 'Footer.jsx',
            path: '/src/components/Footer.jsx',
            created_at: 4000,
          },
        }),
      });
    });

    expect(screen.getByText('Footer.jsx')).toBeInTheDocument();
    expect(screen.getByTestId('artifacts-count-badge')).toHaveTextContent('5');
  });

  test('toggles between Table and Grid view layouts', () => {
    render(
      <GlobalArtifactsView
        artifacts={sampleArtifacts}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    // Initial default layout
    expect(screen.getByTestId('view-mode-table')).toBeInTheDocument();

    // Switch to Grid
    const gridBtn = screen.getByTestId('switch-view-grid');
    fireEvent.click(gridBtn);
    expect(screen.getByTestId('view-mode-grid')).toBeInTheDocument();

    // Switch back to Table
    const tableBtn = screen.getByTestId('switch-view-table');
    fireEvent.click(tableBtn);
    expect(screen.getByTestId('view-mode-table')).toBeInTheDocument();
  });

  test('renders empty state when there are no artifacts recorded', () => {
    render(
      <GlobalArtifactsView
        artifacts={[]}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    expect(screen.getByTestId('empty-artifacts-state')).toBeInTheDocument();
    expect(screen.getByText(/No artifacts found/i)).toBeInTheDocument();
  });

  test('switches between Deliverables and Media Library sections', () => {
    render(
      <GlobalArtifactsView
        artifacts={sampleArtifacts}
        sessions={sampleSessions}
        onNavigateToSession={jest.fn()}
      />
    );

    // Initial section is artifacts / deliverables
    expect(screen.getByTestId('section-tab-artifacts')).toBeInTheDocument();
    const mediaTab = screen.getByTestId('section-tab-media');
    expect(mediaTab).toBeInTheDocument();
    expect(screen.getByTestId('view-mode-table')).toBeInTheDocument();

    // Switch to Media Library section
    fireEvent.click(mediaTab);
    expect(screen.getByTestId('media-library-view')).toBeInTheDocument();
    expect(screen.queryByTestId('view-mode-table')).not.toBeInTheDocument();

    // Switch back to Deliverables section
    const artifactsTab = screen.getByTestId('section-tab-artifacts');
    fireEvent.click(artifactsTab);
    expect(screen.queryByTestId('media-library-view')).not.toBeInTheDocument();
    expect(screen.getByTestId('view-mode-table')).toBeInTheDocument();
  });
});
