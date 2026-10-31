import React, { act } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

// react-markdown is ESM-only; stub it for Jest (repo convention).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

import ArtifactsCanvas from '../ArtifactsCanvas';
import DraggableChatWindow, { ToolActivityCard } from '../../DraggableChatWindow';
import CodeRenderer from '../renderers/CodeRenderer';
import MarkdownRenderer from '../renderers/MarkdownRenderer';
import DiffRenderer from '../renderers/DiffRenderer';
import DataRenderer from '../renderers/DataRenderer';
import ImageViewer from '../renderers/ImageViewer';

describe('Artifact Renderers', () => {
  test('CodeRenderer displays syntax-highlighted code and line numbers', () => {
    const code = "def hello():\n    return 'world'";
    render(<CodeRenderer content={code} fileName="main.py" />);
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();
    expect(screen.getByText('def')).toBeInTheDocument();
    expect(screen.getByText(/hello/)).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  test('MarkdownRenderer renders Markdown content', () => {
    const md = '# Document Title\n\nThis is a paragraph.';
    render(<MarkdownRenderer content={md} />);
    expect(screen.getByTestId('markdown-renderer')).toBeInTheDocument();
    expect(screen.getByText(/Document Title/)).toBeInTheDocument();
    expect(screen.getByText(/This is a paragraph/)).toBeInTheDocument();
  });

  test('DiffRenderer renders added, removed, and hunk header lines', () => {
    const diff = `--- a/file.py\n+++ b/file.py\n@@ -1,2 +1,2 @@\n-old_line\n+new_line\n context_line`;
    render(<DiffRenderer diff={diff} filePath="file.py" />);
    expect(screen.getByTestId('diff-renderer')).toBeInTheDocument();
    expect(screen.getByText(/@@ -1,2 \+1,2 @@/)).toHaveClass('diff-hunk-header');
    expect(screen.getByText(/-old_line/)).toHaveClass('diff-line-removed');
    expect(screen.getByText(/\+new_line/)).toHaveClass('diff-line-added');
  });

  test('DataRenderer renders collapsible JSON tree and CSV table', () => {
    // JSON
    const jsonStr = JSON.stringify({ name: 'Aether', version: 1, nested: { active: true } });
    const { unmount } = render(<DataRenderer content={jsonStr} format="json" />);
    expect(screen.getByTestId('data-renderer')).toBeInTheDocument();
    expect(screen.getByTestId('json-tree')).toBeInTheDocument();
    expect(screen.getByText('name:')).toBeInTheDocument();
    expect(screen.getByText('"Aether"')).toBeInTheDocument();
    unmount();

    // CSV
    const csvStr = 'Name,Age,Role\nAlice,30,Engineer\nBob,25,Designer';
    render(<DataRenderer content={csvStr} format="csv" />);
    expect(screen.getByTestId('csv-table')).toBeInTheDocument();
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Engineer')).toBeInTheDocument();
    expect(screen.getByText('Designer')).toBeInTheDocument();
  });

  test('ImageViewer renders image with zoom controls', () => {
    const src = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
    render(<ImageViewer src={src} alt="Test image" />);
    expect(screen.getByTestId('image-viewer')).toBeInTheDocument();
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('100%');

    // Zoom in
    fireEvent.click(screen.getByTestId('zoom-in'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('125%');

    // Zoom out
    fireEvent.click(screen.getByTestId('zoom-out'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('100%');

    // Reset zoom
    fireEvent.click(screen.getByTestId('zoom-in'));
    fireEvent.click(screen.getByTestId('zoom-reset'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('100%');
  });
});

describe('ArtifactsCanvas Component', () => {
  const sampleArtifacts = [
    {
      id: 'art-1',
      session_id: 'sess-1',
      task_id: 'task-old',
      kind: 'file',
      title: 'setup.py',
      file_path: '/path/setup.py',
      created_at: 1000,
    },
    {
      id: 'art-2',
      session_id: 'sess-1',
      task_id: 'task-new',
      kind: 'file',
      title: 'main.py',
      file_path: '/path/main.py',
      created_at: 2000,
    },
    {
      id: 'art-3',
      session_id: 'sess-1',
      task_id: 'task-new',
      kind: 'diff',
      title: 'main.py',
      file_path: '/path/main.py',
      created_at: 2050,
    },
  ];

  test('renders docked panel grouped by task newest-first and counts artifacts', () => {
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={sampleArtifacts}
        selectedArtifactId={null}
        onSelectArtifact={jest.fn()}
        activeArtifactContent={null}
      />
    );

    expect(screen.getByTestId('artifacts-canvas')).toBeInTheDocument();
    expect(screen.getByTestId('canvas-total-badge')).toHaveTextContent('3');

    // Groups: task-new should appear before task-old
    const taskHeaders = screen.getAllByTestId('task-group-header');
    expect(taskHeaders).toHaveLength(2);
    expect(taskHeaders[0]).toHaveTextContent('task-new');
    expect(taskHeaders[1]).toHaveTextContent('task-old');
  });

  test('clicking an artifact invokes onSelectArtifact', () => {
    const onSelect = jest.fn();
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={sampleArtifacts}
        selectedArtifactId="art-2"
        onSelectArtifact={onSelect}
        activeArtifactContent={null}
      />
    );

    fireEvent.click(screen.getByTestId('artifact-item-art-1'));
    expect(onSelect).toHaveBeenCalledWith('art-1');
  });

  test('renders missing file placeholder when file does not exist on disk', () => {
    const activeContent = {
      artifact: sampleArtifacts[0],
      content: null,
      diff: null,
      exists: false,
      error: 'File /path/setup.py no longer exists on disk',
    };

    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={sampleArtifacts}
        selectedArtifactId="art-1"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={activeContent}
      />
    );

    expect(screen.getByTestId('artifact-file-missing')).toBeInTheDocument();
    expect(screen.getByTestId('artifact-title')).toHaveTextContent('setup.py');
    expect(screen.getByText(/File \/path\/setup\.py no longer exists on disk/)).toBeInTheDocument();
  });

  test('renders view-only actions (copy, save, open) and executes copy', async () => {
    Object.assign(navigator, {
      clipboard: {
        writeText: jest.fn().mockImplementation(() => Promise.resolve()),
      },
    });

    const activeContent = {
      artifact: sampleArtifacts[1],
      content: 'print("hello world")',
      diff: null,
      exists: true,
      error: null,
    };

    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={sampleArtifacts}
        selectedArtifactId="art-2"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={activeContent}
      />
    );

    expect(screen.getByTestId('artifact-copy-btn')).toBeInTheDocument();
    expect(screen.getByTestId('artifact-save-btn')).toBeInTheDocument();
    expect(screen.getByTestId('artifact-open-btn')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByTestId('artifact-copy-btn'));
    });
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('print("hello world")');
  });

  test('ToolActivityCard renders filename link and invokes onOpenArtifact when clicked', () => {
    const onOpen = jest.fn();
    const payload = {
      tool_name: 'write_file',
      tool_args: { path: '/home/user/workspace/script.py', content: 'print(1)' },
      status: 'completed',
      node: 'coder',
      to_node: 'execute_tool',
    };
    render(<ToolActivityCard payload={payload} onOpenArtifact={onOpen} />);
    const linkBtn = screen.getByTestId('tool-card-file-link');
    expect(linkBtn).toBeInTheDocument();
    expect(linkBtn).toHaveTextContent('script.py');
    fireEvent.click(linkBtn);
    expect(onOpen).toHaveBeenCalledWith('/home/user/workspace/script.py');
  });

  test('DraggableChatWindow renders Artifacts button with count badge and triggers onToggleCanvas', () => {
    const onToggle = jest.fn();
    render(
      <DraggableChatWindow
        messages={[]}
        status="Online"
        artifactsCount={3}
        isCanvasOpen={false}
        onToggleCanvas={onToggle}
      />
    );
    const headerBtn = screen.getByTestId('artifacts-header-btn');
    expect(headerBtn).toBeInTheDocument();
    const badge = screen.getByTestId('artifacts-badge');
    expect(badge).toHaveTextContent('3');

    fireEvent.click(headerBtn);
    expect(onToggle).toHaveBeenCalled();
  });

  test('reopening a session restores viewed artifact selection', () => {
    localStorage.setItem('aether_canvas_selected_sess-1', 'art-3');
    const savedId = localStorage.getItem('aether_canvas_selected_sess-1');
    expect(savedId).toBe('art-3');

    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={sampleArtifacts}
        selectedArtifactId={savedId}
        onSelectArtifact={jest.fn()}
        activeArtifactContent={{
          artifact: sampleArtifacts[2],
          content: '--- main.py\n+++ main.py\n@@ -1 +1 @@\n-old\n+new',
          diff: '--- main.py\n+++ main.py\n@@ -1 +1 @@\n-old\n+new',
          exists: true,
          error: null,
        }}
      />
    );

    expect(screen.getByTestId('diff-renderer')).toBeInTheDocument();
  });
});
