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
import { artifactSavePayload } from '../artifactSave';
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

describe('N06 — artifactSavePayload preserves real bytes and MIME', () => {
  const pngArtifact = {
    id: 'art-img', session_id: 'sess-1', task_id: 'task-1', kind: 'file',
    title: 'photo.png', file_path: '/path/photo.png', created_at: 3000,
  };

  test('image data URL decodes to the real bytes with the image MIME type', () => {
    // 'iVBORw0KGgo=' is the 8-byte PNG signature.
    const payload = artifactSavePayload(pngArtifact, {
      artifact: pngArtifact,
      content: 'data:image/png;base64,iVBORw0KGgo=',
      diff: null, exists: true, error: null,
    });
    expect(payload).not.toBeNull();
    expect(payload.mimeType).toBe('image/png');
    expect(payload.filename).toBe('photo.png');
    expect(payload.bytes).toHaveLength(8);
    expect(payload.bytes[0]).toBe(0x89);
    expect(payload.bytes[1]).toBe(0x50); // 'P'
  });

  test('jpeg data URL keeps image/jpeg, never text/plain', () => {
    const payload = artifactSavePayload(
      { ...pngArtifact, title: 'shot.jpg', file_path: '/path/shot.jpg' },
      { content: 'data:image/jpeg;base64,/9j/4AAQ', diff: null, exists: true, error: null },
    );
    expect(payload.mimeType).toBe('image/jpeg');
    expect(payload.bytes[0]).toBe(0xff);
    expect(payload.bytes[1]).toBe(0xd8);
  });

  test('non-base64 svg data URL decodes its text with image/svg+xml', () => {
    const payload = artifactSavePayload(
      { ...pngArtifact, title: 'icon.svg', file_path: '/path/icon.svg' },
      { content: 'data:image/svg+xml;utf8,%3Csvg%3E%3C%2Fsvg%3E', diff: null, exists: true, error: null },
    );
    expect(payload.mimeType).toBe('image/svg+xml');
    expect(String.fromCharCode(...payload.bytes)).toBe('<svg></svg>');
  });

  test('text artifacts take their MIME type from the file extension', () => {
    const md = artifactSavePayload(
      { ...pngArtifact, title: 'notes.md', file_path: '/path/notes.md' },
      { content: '# Hi', diff: null, exists: true, error: null },
    );
    expect(md.mimeType).toBe('text/markdown');
    expect(Array.from(md.bytes)).toEqual([35, 32, 72, 105]);

    const json = artifactSavePayload(
      { ...pngArtifact, title: 'data.json', file_path: '/path/data.json' },
      { content: '{"a":1}', diff: null, exists: true, error: null },
    );
    expect(json.mimeType).toBe('application/json');
  });

  test('diff artifacts save the diff text; reports without an extension gain .md', () => {
    const diffPayload = artifactSavePayload(
      { ...pngArtifact, kind: 'diff', title: 'main.py', file_path: '/path/main.py' },
      { content: '', diff: '--- a\n+++ b', exists: true, error: null },
    );
    expect(diffPayload.mimeType).toBe('text/plain');
    expect(String.fromCharCode(...diffPayload.bytes)).toBe('--- a\n+++ b');

    const report = artifactSavePayload(
      { ...pngArtifact, kind: 'report', title: 'Summary', file_path: null },
      { content: '# Summary', diff: null, exists: true, error: null },
    );
    expect(report.filename).toBe('Summary.md');
    expect(report.mimeType).toBe('text/markdown');
  });

  test('empty content yields no payload (nothing fabricated to save)', () => {
    expect(artifactSavePayload(pngArtifact, { content: '', diff: null, exists: true, error: null })).toBeNull();
    expect(artifactSavePayload(pngArtifact, null)).toBeNull();
  });
});

describe('N06 — canvas actions and docking', () => {
  const artifacts = [
    {
      id: 'art-2', session_id: 'sess-1', task_id: 'task-new', kind: 'file',
      title: 'main.py', file_path: '/path/main.py', created_at: 2000,
    },
    {
      id: 'art-img', session_id: 'sess-1', task_id: 'task-new', kind: 'file',
      title: 'photo.png', file_path: '/path/photo.png', created_at: 2100,
    },
  ];

  afterEach(() => {
    delete window.electronAPI;
    jest.restoreAllMocks();
  });

  test('panel docks in the layout flow instead of floating as an absolute overlay', () => {
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId={null}
        onSelectArtifact={jest.fn()}
        activeArtifactContent={null}
      />
    );
    const panel = screen.getByTestId('artifacts-canvas');
    expect(panel.style.position).not.toBe('absolute');
    expect(panel.style.position).not.toBe('fixed');
    expect(panel.style.top).toBe('');
    expect(panel.style.right).toBe('');
    expect(panel.style.bottom).toBe('');
    expect(panel.style.width).toBe('480px');
    expect(panel.style.flexShrink).toBe('0');
  });

  test('Save a copy downloads a Blob with the artifact MIME type and bytes', async () => {
    const createObjectURL = jest.fn(() => 'blob:mock-url');
    const revokeObjectURL = jest.fn();
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;
    let anchorEl = null;
    jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { anchorEl = this; });

    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId="art-img"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={{
          artifact: artifacts[1],
          content: 'data:image/png;base64,iVBORw0KGgo=',
          diff: null, exists: true, error: null,
        }}
      />
    );

    await act(async () => { fireEvent.click(screen.getByTestId('artifact-save-btn')); });

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0][0];
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe('image/png');
    expect(blob.size).toBe(8);
    expect(anchorEl).not.toBeNull();
    expect(anchorEl.download).toBe('photo.png');
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url');
  });

  test('Open externally hands the file path to the OS shell seam, not a picker', async () => {
    const openPath = jest.fn().mockResolvedValue('');
    const showOpenDialog = jest.fn();
    window.electronAPI = { openPath, showOpenDialog };

    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId="art-2"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={{
          artifact: artifacts[0],
          content: 'print("hello")', diff: null, exists: true, error: null,
        }}
      />
    );

    await act(async () => { fireEvent.click(screen.getByTestId('artifact-open-btn')); });

    expect(openPath).toHaveBeenCalledTimes(1);
    expect(openPath).toHaveBeenCalledWith('/path/main.py');
    expect(showOpenDialog).not.toHaveBeenCalled();
    expect(screen.queryByTestId('artifact-open-error')).not.toBeInTheDocument();
  });

  test('Open externally surfaces a shell error instead of pretending success', async () => {
    window.electronAPI = { openPath: jest.fn().mockResolvedValue('No application found to open the file.') };

    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId="art-2"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={{
          artifact: artifacts[0],
          content: 'print("hello")', diff: null, exists: true, error: null,
        }}
      />
    );

    await act(async () => { fireEvent.click(screen.getByTestId('artifact-open-btn')); });

    expect(screen.getByTestId('artifact-open-error')).toHaveTextContent('No application found to open the file.');
  });
});
