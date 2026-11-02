import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

// react-markdown is ESM-only; stub it for Jest (repo convention).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

import ProjectPreview from '../renderers/ProjectPreview';
import ArtifactsCanvas from '../ArtifactsCanvas';

// Static (srcDoc) previews: scripts run but the origin is opaque (M01).
const SANDBOX = 'allow-scripts';
// Server previews: isolated <webview> guest, own session partition (M01).
const PREVIEW_PARTITION = 'aether-preview';

const htmlArtifact = {
  id: 'art-html',
  session_id: 'sess-1',
  task_id: 'task-1',
  kind: 'file',
  title: 'index.html',
  file_path: '/site/index.html',
  created_at: 1000,
};

describe('ProjectPreview', () => {
  test('renders the detected dev server in an exactly-sandboxed iframe', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={null}
        devServer={{ running: true, port: 5173, url: 'http://localhost:5173', framework: 'Vite' }}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame).toBeInTheDocument();
    // M01: server previews run in an isolated webview guest (separate
    // WebContents + session partition), never an in-window iframe.
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('partition')).toBe(PREVIEW_PARTITION);
    expect(frame.getAttribute('src')).toBe('http://localhost:5173');
    expect(screen.getByTestId('preview-framework')).toHaveTextContent('Vite');
    expect(screen.getByTestId('preview-url-input')).toHaveValue('http://localhost:5173');
  });

  test('port selector switches the iframe URL', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={null}
        devServer={{ running: true, port: 5173, url: 'http://localhost:5173', framework: 'Vite' }}
        onDetect={jest.fn()}
      />
    );

    fireEvent.change(screen.getByTestId('preview-port-select'), { target: { value: '3000' } });

    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.getAttribute('src')).toBe('http://localhost:3000');
    expect(screen.getByTestId('preview-url-input')).toHaveValue('http://localhost:3000');
  });

  test('URL bar navigates the iframe to a typed address', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={null}
        devServer={{ running: true, port: 5173, url: 'http://localhost:5173', framework: null }}
        onDetect={jest.fn()}
      />
    );

    const input = screen.getByTestId('preview-url-input');
    fireEvent.change(input, { target: { value: 'localhost:8080/app' } });
    fireEvent.click(screen.getByTestId('preview-go-btn'));

    expect(screen.getByTestId('project-preview-iframe').getAttribute('src')).toBe('http://localhost:8080/app');
  });

  test('reload remounts the iframe and re-runs detection', () => {
    const onDetect = jest.fn();
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={null}
        devServer={{ running: true, port: 5173, url: 'http://localhost:5173', framework: null }}
        onDetect={onDetect}
      />
    );

    const before = screen.getByTestId('project-preview-iframe');
    fireEvent.click(screen.getByTestId('preview-reload-btn'));
    const after = screen.getByTestId('project-preview-iframe');

    expect(after).not.toBe(before);
    expect(after.getAttribute('src')).toBe('http://localhost:5173');
    expect(onDetect).toHaveBeenCalled();
  });

  test('static HTML artifact WITH a file on disk renders via the isolated webview guest at the file:// URL (FUP-2)', () => {
    const html = '<!DOCTYPE html><html><body><h1>Hello</h1></body></html>';
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={html}
        devServer={{ running: false, port: null, url: null, framework: null }}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    // FUP-2: the static preview goes through the SAME guest route as
    // server mode, pointed at the artifact's real file, so the page
    // renders exactly as authored (inline scripts run, relative
    // sibling assets resolve) and the app window's CSP — which a
    // srcDoc iframe inherits — no longer applies to it.
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('partition')).toBe(PREVIEW_PARTITION);
    expect(frame.getAttribute('src')).toBe('file:///site/index.html');
    // No content is piped through the renderer at all on this path:
    // no srcDoc, no sandbox attribute, no transformation.
    expect(frame.getAttribute('srcdoc')).toBeFalsy();
    expect(frame.getAttribute('sandbox')).toBeFalsy();
    expect(frame.getAttribute('preload')).toBeNull();
    // The URL bar still shows the artifact's real path.
    expect(screen.getByTestId('preview-url-input')).toHaveValue('/site/index.html');
  });

  test('static file URL percent-encodes path segments (spaces, #) without touching separators', () => {
    const spaced = { ...htmlArtifact, file_path: '/site/my folder/page #1.html' };
    render(
      <ProjectPreview
        artifact={spaced}
        content={'<html><body>hi</body></html>'}
        devServer={{ running: false, port: null, url: null, framework: null }}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('src')).toBe('file:///site/my%20folder/page%20%231.html');
  });

  test('static file URL handles Windows-shaped artifact paths (backslashes, drive letter)', () => {
    const win = { ...htmlArtifact, file_path: 'C:\\Users\\arindam\\site\\index page.html' };
    render(
      <ProjectPreview
        artifact={win}
        content={'<html><body>hi</body></html>'}
        devServer={{ running: false, port: null, url: null, framework: null }}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('src')).toBe('file:///C:/Users/arindam/site/index%20page.html');
  });

  test('reload remounts the static file webview and re-runs detection', () => {
    const onDetect = jest.fn();
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={'<html><body>hi</body></html>'}
        devServer={{ running: false, port: null, url: null, framework: null }}
        onDetect={onDetect}
      />
    );

    const before = screen.getByTestId('project-preview-iframe');
    fireEvent.click(screen.getByTestId('preview-reload-btn'));
    const after = screen.getByTestId('project-preview-iframe');

    expect(after).not.toBe(before);
    expect(after.tagName).toBe('WEBVIEW');
    expect(after.getAttribute('src')).toBe('file:///site/index.html');
    expect(onDetect).toHaveBeenCalled();
  });

  test('static HTML artifact WITHOUT a file_path keeps the srcDoc iframe behaviour (FUP-2 fallback)', () => {
    const contentOnly = { ...htmlArtifact, file_path: null };
    const html = '<!DOCTYPE html><html><body><h1>Hello</h1></body></html>';
    render(
      <ProjectPreview
        artifact={contentOnly}
        content={html}
        devServer={{ running: false, port: null, url: null, framework: null }}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    // There is no file to be faithful to, so the pre-FUP-2 route
    // stays: sandboxed srcDoc iframe, scripts on, opaque origin.
    expect(frame.tagName).toBe('IFRAME');
    expect(frame.getAttribute('sandbox')).toBe(SANDBOX);
    expect(frame.getAttribute('srcdoc')).toBe(html);
    expect(frame.getAttribute('src')).toBeFalsy();
  });

  test('unreachable dev server with no static content shows the fallback, port pick recovers', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={null}
        devServer={{ running: false, port: null, url: null, framework: null }}
        onDetect={jest.fn()}
      />
    );

    expect(screen.getByTestId('preview-no-server')).toBeInTheDocument();
    expect(screen.queryByTestId('project-preview-iframe')).not.toBeInTheDocument();

    // The fallback is actionable: choosing a port loads it directly.
    fireEvent.change(screen.getByTestId('preview-port-select'), { target: { value: '8000' } });
    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.getAttribute('src')).toBe('http://localhost:8000');
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('partition')).toBe(PREVIEW_PARTITION);
  });
});

describe('ArtifactsCanvas Preview tab (E8-2.2)', () => {
  const artifacts = [
    htmlArtifact,
    {
      id: 'art-py',
      session_id: 'sess-1',
      task_id: 'task-1',
      kind: 'file',
      title: 'main.py',
      file_path: '/site/main.py',
      created_at: 900,
    },
    {
      id: 'art-md',
      session_id: 'sess-1',
      task_id: 'task-1',
      kind: 'file',
      title: 'notes.md',
      file_path: '/site/notes.md',
      created_at: 800,
    },
  ];

  const contentFor = (artifact, content) => ({
    artifact, content, diff: null, exists: true, error: null,
  });

  test('web artifact shows Code + Preview tabs; Preview renders the static page and requests detection', () => {
    const onDetect = jest.fn();
    const html = '<html><body>hi</body></html>';
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId="art-html"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={contentFor(htmlArtifact, html)}
        devServerInfo={null}
        onDetectDevServer={onDetect}
      />
    );

    expect(screen.getByTestId('artifact-tab-code')).toBeInTheDocument();
    expect(screen.getByTestId('artifact-tab-preview')).toBeInTheDocument();
    // Code is the default view.
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();
    expect(screen.queryByTestId('project-preview-iframe')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('artifact-tab-preview'));

    // FUP-2: the artifact has a real file_path, so the Preview tab
    // loads that file through the isolated webview guest.
    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('partition')).toBe(PREVIEW_PARTITION);
    expect(frame.getAttribute('src')).toBe('file:///site/index.html');
    expect(frame.getAttribute('srcdoc')).toBeFalsy();
    expect(onDetect).toHaveBeenCalledWith({ workspace_root: null, project_path: '/site' });
  });

  test('detected dev server URL wins over static content in the Preview tab', () => {
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId="art-html"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={contentFor(htmlArtifact, '<html></html>')}
        devServerInfo={{ running: true, port: 3000, url: 'http://localhost:3000', framework: 'Next.js' }}
        onDetectDevServer={jest.fn()}
      />
    );

    fireEvent.click(screen.getByTestId('artifact-tab-preview'));
    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.getAttribute('src')).toBe('http://localhost:3000');
    expect(frame.getAttribute('srcdoc')).toBeFalsy();
  });

  test('non-web artifacts show no Preview tab', () => {
    const pyArtifact = artifacts[1];
    const { rerender } = render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId="art-py"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={contentFor(pyArtifact, 'print(1)')}
        devServerInfo={null}
        onDetectDevServer={jest.fn()}
      />
    );

    expect(screen.queryByTestId('artifact-tab-preview')).not.toBeInTheDocument();
    expect(screen.queryByTestId('artifact-tab-code')).not.toBeInTheDocument();
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();

    const mdArtifact = artifacts[2];
    rerender(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId="art-md"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={contentFor(mdArtifact, '# Notes')}
        devServerInfo={null}
        onDetectDevServer={jest.fn()}
      />
    );

    expect(screen.queryByTestId('artifact-tab-preview')).not.toBeInTheDocument();
    expect(screen.getByTestId('markdown-renderer')).toBeInTheDocument();
  });

  test('missing web file shows the missing placeholder and no tabs', () => {
    render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={artifacts}
        selectedArtifactId="art-html"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={{
          artifact: htmlArtifact, content: null, diff: null,
          exists: false, error: 'File /site/index.html no longer exists on disk',
        }}
        devServerInfo={null}
        onDetectDevServer={jest.fn()}
      />
    );

    expect(screen.getByTestId('artifact-file-missing')).toBeInTheDocument();
    expect(screen.queryByTestId('artifact-tab-preview')).not.toBeInTheDocument();
  });

  test('switching artifacts resets the viewer to the Code tab', () => {
    const otherHtml = {
      ...htmlArtifact, id: 'art-html-2', title: 'about.html', file_path: '/site/about.html', created_at: 1100,
    };
    const all = [otherHtml, ...artifacts];
    const { rerender } = render(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={all}
        selectedArtifactId="art-html"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={contentFor(htmlArtifact, '<html><body>one</body></html>')}
        devServerInfo={null}
        onDetectDevServer={jest.fn()}
      />
    );

    fireEvent.click(screen.getByTestId('artifact-tab-preview'));
    expect(screen.getByTestId('project-preview-iframe')).toBeInTheDocument();

    rerender(
      <ArtifactsCanvas
        isOpen={true}
        onClose={jest.fn()}
        artifacts={all}
        selectedArtifactId="art-html-2"
        onSelectArtifact={jest.fn()}
        activeArtifactContent={contentFor(otherHtml, '<html><body>two</body></html>')}
        devServerInfo={null}
        onDetectDevServer={jest.fn()}
      />
    );

    expect(screen.queryByTestId('project-preview-iframe')).not.toBeInTheDocument();
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();
  });
});
