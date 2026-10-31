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

// jsdom has no URL.createObjectURL; stub it.
if (typeof URL.createObjectURL === 'undefined') {
  URL.createObjectURL = jest.fn(() => 'blob:mock-url');
}
if (typeof URL.revokeObjectURL === 'undefined') {
  URL.revokeObjectURL = jest.fn();
}

import ArtifactsCanvas, { SummaryDocumentRenderer } from '../ArtifactsCanvas';

const PREVIEW_SANDBOX = 'allow-scripts allow-same-origin';

describe('SummaryDocumentRenderer & Summary Document Artifacts (E8-2.4)', () => {
  const summaryArtifact = {
    id: 'art-report-1',
    session_id: 'sess-1',
    task_id: 'task-101',
    kind: 'report',
    title: 'Task Summary',
    file_path: null,
    created_at: 5000,
  };

  const sampleArtifacts = [
    summaryArtifact,
    {
      id: 'art-code-1',
      session_id: 'sess-1',
      task_id: 'task-101',
      kind: 'file',
      title: 'app.py',
      file_path: '/project/app.py',
      created_at: 4000,
    },
  ];

  test('renders summary document using MarkdownRenderer and displays report badge', () => {
    const summaryText = '# Task Completed\n\n- Created database schema\n- Generated summary report';
    const activeContent = {
      artifact: summaryArtifact,
      content: summaryText,
      diff: null,
      exists: true,
      error: null,
    };

    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={sampleArtifacts}
        selectedArtifactId="art-report-1"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={activeContent}
      />
    );

    // Kind badge displays REPORT
    const reportItem = screen.getByTestId('artifact-item-art-report-1');
    expect(reportItem).toBeInTheDocument();
    expect(reportItem).toHaveTextContent(/report/i);
    expect(reportItem).toHaveTextContent('Task Summary');

    // Renders with MarkdownRenderer, NOT CodeRenderer
    expect(screen.getByTestId('markdown-renderer')).toBeInTheDocument();
    expect(screen.queryByTestId('code-renderer')).not.toBeInTheDocument();
    expect(screen.getByTestId('markdown-stub')).toHaveTextContent(/Task Completed/);
    expect(screen.getByTestId('markdown-stub')).toHaveTextContent(/Created database schema/);
  });

  test('SummaryDocumentRenderer component renders markdown content directly', () => {
    const text = '## Direct Component Render\n\nSummary text content.';
    render(<SummaryDocumentRenderer content={text} title="Task Summary" />);

    expect(screen.getByTestId('summary-document-renderer')).toBeInTheDocument();
    expect(screen.getByTestId('markdown-renderer')).toBeInTheDocument();
    expect(screen.getByTestId('markdown-stub')).toHaveTextContent(/Direct Component Render/);
    expect(screen.getByTestId('markdown-stub')).toHaveTextContent(/Summary text content/);
  });

  test('view-only actions copy and save summary document content', async () => {
    Object.assign(navigator, {
      clipboard: {
        writeText: jest.fn().mockImplementation(() => Promise.resolve()),
      },
    });

    const summaryText = 'Final summary documentation.';
    const activeContent = {
      artifact: summaryArtifact,
      content: summaryText,
      diff: null,
      exists: true,
      error: null,
    };

    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={sampleArtifacts}
        selectedArtifactId="art-report-1"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={activeContent}
      />
    );

    // Copy action
    await act(async () => {
      fireEvent.click(screen.getByTestId('artifact-copy-btn'));
    });
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(summaryText);

    // Save action
    const createElementSpy = jest.spyOn(document, 'createElement');
    fireEvent.click(screen.getByTestId('artifact-save-btn'));
    expect(createElementSpy).toHaveBeenCalledWith('a');
    createElementSpy.mockRestore();
  });
});

describe('Standalone HTML preview toggle semantics (E8-2.4)', () => {
  const standaloneHtmlArtifact = {
    id: 'art-standalone-html',
    session_id: 'sess-html',
    task_id: 'task-html',
    kind: 'html',
    title: 'standalone.html',
    file_path: '/standalone.html',
    created_at: 6000,
  };

  const htmlContent = '<!DOCTYPE html><html><head><title>Test</title></head><body><h1>Interactive</h1></body></html>';

  test('standalone HTML artifact defaults to code view', () => {
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={[standaloneHtmlArtifact]}
        selectedArtifactId="art-standalone-html"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={{
          artifact: standaloneHtmlArtifact,
          content: htmlContent,
          diff: null,
          exists: true,
          error: null,
        }}
        devServerInfo={null}
        onDetectDevServer={jest.fn()}
      />
    );

    // Code is the default view
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();
    expect(screen.queryByTestId('project-preview-iframe')).not.toBeInTheDocument();

    // Both tabs are visible for toggling
    expect(screen.getByTestId('artifact-tab-code')).toBeInTheDocument();
    expect(screen.getByTestId('artifact-tab-preview')).toBeInTheDocument();
  });

  test('toggling preview renders sandboxed iframe with allow-scripts allow-same-origin', () => {
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={[standaloneHtmlArtifact]}
        selectedArtifactId="art-standalone-html"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={{
          artifact: standaloneHtmlArtifact,
          content: htmlContent,
          diff: null,
          exists: true,
          error: null,
        }}
        devServerInfo={null}
        onDetectDevServer={jest.fn()}
      />
    );

    // Toggle to preview
    fireEvent.click(screen.getByTestId('artifact-tab-preview'));

    const iframe = screen.getByTestId('project-preview-iframe');
    expect(iframe).toBeInTheDocument();
    expect(iframe.getAttribute('sandbox')).toBe(PREVIEW_SANDBOX);
    expect(iframe.getAttribute('srcdoc')).toBe(htmlContent);
    expect(screen.queryByTestId('code-renderer')).not.toBeInTheDocument();

    // Toggle back to code
    fireEvent.click(screen.getByTestId('artifact-tab-code'));
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();
    expect(screen.queryByTestId('project-preview-iframe')).not.toBeInTheDocument();
  });
});
