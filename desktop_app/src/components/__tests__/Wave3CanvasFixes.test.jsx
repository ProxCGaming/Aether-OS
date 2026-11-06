import React, { act } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

// react-markdown is ESM-only; stub it for Jest (repo convention).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

import ArtifactsCanvas from '../ArtifactsCanvas';

// Wave 3 (D3 chunk) — ArtifactsCanvas correctness:
//   D3-17 — viewer routing must consider file_path, not title only,
//           and the icon / viewer extension sets must agree.
//   D3-18 — a failed clipboard copy must surface feedback.
//   D3-19 — the loading prop must render a loading state.
//   D3-06 — a malformed payload must not make "Save a copy" die
//           silently (payload returns null; nothing downloads, and
//           no exception escapes the click).

const baseArtifacts = [
  {
    id: 'art-md', session_id: 'sess-1', task_id: 'task-1', kind: 'file',
    title: 'README', file_path: '/docs/README.md', created_at: 1000,
  },
  {
    id: 'art-bmp', session_id: 'sess-1', task_id: 'task-1', kind: 'file',
    title: 'pic.bmp', file_path: '/img/pic.bmp', created_at: 1100,
  },
  {
    id: 'art-json', session_id: 'sess-1', task_id: 'task-1', kind: 'file',
    title: 'data', file_path: '/x/data.json', created_at: 1200,
  },
];

function renderCanvas(artifact, content, extraProps = {}) {
  return render(
    <ArtifactsCanvas
      isOpen={true}
      onClose={jest.fn()}
      artifacts={baseArtifacts}
      selectedArtifactId={artifact.id}
      onSelectArtifact={jest.fn()}
      activeArtifactContent={{ artifact, content, diff: null, exists: true, error: null }}
      {...extraProps}
    />
  );
}

describe('D3-17 — routing and icons agree, and use file_path', () => {
  it('routes an artifact titled README whose file is README.md to the markdown viewer', () => {
    renderCanvas(baseArtifacts[0], '# Hello');
    expect(screen.getByTestId('markdown-stub')).toBeInTheDocument();
    expect(screen.queryByTestId('code-renderer')).not.toBeInTheDocument();
  });

  it('routes an artifact whose file_path ends .json to the data viewer', () => {
    renderCanvas(baseArtifacts[2], '{"a": 1}');
    expect(screen.getByTestId('json-tree')).toBeInTheDocument();
  });

  it('gives a .bmp artifact the image icon, matching its image viewer', () => {
    const { container } = renderCanvas(baseArtifacts[1], 'data:image/bmp;base64,xxx');
    expect(screen.getByTestId('image-viewer')).toBeInTheDocument();
    const item = screen.getByTestId('artifact-item-art-bmp');
    expect(item.querySelector('.lucide-image')).not.toBeNull();
    expect(container).toBeTruthy();
  });
});

describe('D3-18 — copy failure is surfaced, not swallowed', () => {
  it('shows copy-failed feedback when the clipboard rejects', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: jest.fn(() => Promise.reject(new Error('denied'))) },
    });
    renderCanvas(baseArtifacts[0], '# Hello');
    await act(async () => { fireEvent.click(screen.getByTestId('artifact-copy-btn')); });
    expect(screen.getByTestId('artifact-copy-btn')).toHaveTextContent(/copy failed/i);
  });

  it('still flips to Copied when the clipboard accepts', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: jest.fn(() => Promise.resolve()) },
    });
    renderCanvas(baseArtifacts[0], '# Hello');
    await act(async () => { fireEvent.click(screen.getByTestId('artifact-copy-btn')); });
    expect(screen.getByTestId('artifact-copy-btn')).toHaveTextContent(/copied/i);
  });
});

describe('D3-19 — the loading state renders while content is in flight', () => {
  it('renders a loading placeholder instead of an empty code view', () => {
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={baseArtifacts}
        selectedArtifactId="art-md"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={null}
        isLoading={true}
      />
    );
    expect(screen.getByTestId('artifact-content-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('code-renderer')).not.toBeInTheDocument();
  });
});

describe('D3-06 — Save a copy with a malformed payload does not throw or fabricate a download', () => {
  it('clicking Save on undecodable base64 content downloads nothing and raises nothing', async () => {
    const createObjectURL = jest.fn(() => 'blob:mock');
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = jest.fn();
    renderCanvas(
      { ...baseArtifacts[1], title: 'photo.png', file_path: '/img/photo.png' },
      'data:image/png;base64,!!!not-base64!!!'
    );
    await act(async () => { fireEvent.click(screen.getByTestId('artifact-save-btn')); });
    expect(createObjectURL).not.toHaveBeenCalled();
  });
});
