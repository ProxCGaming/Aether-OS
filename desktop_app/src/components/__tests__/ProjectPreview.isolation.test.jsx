import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import fs from 'fs';
import path from 'path';

// react-markdown is ESM-only; stub it for Jest (repo convention).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

import ProjectPreview from '../renderers/ProjectPreview';

// M01 regression suite — artifact preview escape to the privileged
// Electron bridge.
//
// jsdom does not enforce the HTML sandbox origin policy and there is
// no Electron runtime in Jest, so these tests assert the isolation
// *configuration invariants* that the HTML/Electron specs turn into
// the actual boundary:
//
//  - A srcDoc document inherits its embedder's origin UNLESS its
//    sandbox omits allow-same-origin (then its origin is opaque and
//    it is cross-origin to the embedder: parent.electronAPI reads
//    throw, parent navigation is blocked, only postMessage remains —
//    and the app never accepts engine messages via postMessage).
//  - An <iframe src> shares the embedder's window tree; whenever its
//    URL's origin coincides with the app's own origin (in dev the
//    shell loads http://localhost:5173 — candidate preview port #1)
//    plus allow-same-origin, its script reaches the parent bridge.
//    A <webview> guest is a separate WebContents with no preload and
//    no shared window tree, so no URL can ever give it that reach.
const CANARY_HTML =
  '<!DOCTYPE html><html><body><script>window.parent.electronAPI.sendEngineMessage("pwned")</script></body></html>';

const htmlArtifact = {
  id: 'art-html',
  session_id: 'sess-1',
  task_id: 'task-1',
  kind: 'file',
  title: 'index.html',
  file_path: '/site/index.html',
  created_at: 1000,
};

// FUP-2: an artifact WITH a file_path previews through the webview
// guest (see the FUP-2 describe below); the srcDoc route — and so
// the srcDoc isolation invariants — now applies only to
// content-only artifacts, which have no file to load.
const contentOnlyArtifact = { ...htmlArtifact, file_path: null };

const noServer = { running: false, port: null, url: null, framework: null };

const sandboxTokens = (el) =>
  (el.getAttribute('sandbox') || '').split(/\s+/).filter(Boolean);

// True when a srcDoc iframe would run SAME-origin with the app
// window — the exact configuration the M01 canary exploited.
const srcDocSharesEmbedderOrigin = (el) =>
  el.tagName === 'IFRAME' &&
  el.hasAttribute('srcdoc') &&
  sandboxTokens(el).includes('allow-same-origin');

describe('M01: static preview isolation', () => {
  test('canary HTML in a srcDoc preview is not same-origin with the app window', () => {
    render(
      <ProjectPreview
        artifact={contentOnlyArtifact}
        content={CANARY_HTML}
        devServer={noServer}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    // The hole, reproduced as a configuration fact: before the fix
    // this is true and the canary script can call
    // window.parent.electronAPI.sendEngineMessage(...).
    expect(srcDocSharesEmbedderOrigin(frame)).toBe(false);
  });

  test('static preview keeps scripts enabled but drops allow-same-origin', () => {
    render(
      <ProjectPreview
        artifact={contentOnlyArtifact}
        content={CANARY_HTML}
        devServer={noServer}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    const tokens = sandboxTokens(frame);
    // Previews must stay functional: scripts still run.
    expect(tokens).toContain('allow-scripts');
    // ...but the framed document's origin must be opaque.
    expect(tokens).not.toContain('allow-same-origin');
    // And it must never be able to navigate the app window.
    expect(tokens).not.toContain('allow-top-navigation');
    expect(tokens).not.toContain('allow-top-navigation-by-user-activation');
  });
});

describe('M01: server preview isolation', () => {
  test('dev-server preview does not render as an in-window iframe — even on the app dev origin port', () => {
    // main.cjs loads the shell itself from http://localhost:5173 in
    // dev; 5173 is also candidate preview port #1. An iframe here
    // would be same-origin with the app window.
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={null}
        devServer={{ running: true, port: 5173, url: 'http://localhost:5173', framework: 'Vite' }}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.tagName).not.toBe('IFRAME');
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('src')).toBe('http://localhost:5173');
  });

  test('server preview guest gets a dedicated session partition and no preload bridge', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={null}
        devServer={{ running: true, port: 3000, url: 'http://localhost:3000', framework: null }}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    // Separate Electron session/storage partition for the guest.
    expect(frame.getAttribute('partition')).toBe('aether-preview');
    // No preload => the guest page itself has no electronAPI at all.
    expect(frame.getAttribute('preload')).toBeNull();
    // No node integration attribute => no Node in the guest.
    expect(frame.getAttribute('nodeintegration')).toBeNull();
  });

  test('a typed remote URL is isolated the same way', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={null}
        devServer={{ running: true, port: 5173, url: 'http://localhost:5173', framework: null }}
        onDetect={jest.fn()}
      />
    );

    const input = screen.getByTestId('preview-url-input');
    fireEvent.change(input, { target: { value: 'https://example.com/page' } });
    fireEvent.click(screen.getByTestId('preview-go-btn'));

    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('partition')).toBe('aether-preview');
    expect(frame.getAttribute('src')).toBe('https://example.com/page');
  });
});

describe('FUP-2: static preview with a file on disk stays bridge-isolated', () => {
  test('file-backed static preview is a webview guest, never an in-window iframe or srcDoc', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={CANARY_HTML}
        devServer={noServer}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    // Same guest route as server mode: separate WebContents, own
    // session partition, no preload — the canary's
    // window.parent.electronAPI reach does not exist here because
    // there is no shared window tree and no bridge in the guest.
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('partition')).toBe('aether-preview');
    expect(frame.getAttribute('preload')).toBeNull();
    expect(frame.getAttribute('nodeintegration')).toBeNull();
    // The artifact content is NOT handed to the frame as a string:
    // the guest loads the file itself, byte for byte.
    expect(frame.hasAttribute('srcdoc')).toBe(false);
    expect(frame.getAttribute('src')).toBe('file:///site/index.html');
  });

  test('server mode still wins over a file-backed static artifact', () => {
    render(
      <ProjectPreview
        artifact={htmlArtifact}
        content={CANARY_HTML}
        devServer={{ running: true, port: 3000, url: 'http://localhost:3000', framework: null }}
        onDetect={jest.fn()}
      />
    );

    const frame = screen.getByTestId('project-preview-iframe');
    expect(frame.tagName).toBe('WEBVIEW');
    expect(frame.getAttribute('src')).toBe('http://localhost:3000');
  });
});

describe('M01: shell support for the isolated server preview', () => {
  test('main.cjs enables webview guests in both app windows', () => {
    const mainSrc = fs.readFileSync(
      path.join(__dirname, '..', '..', '..', 'main.cjs'),
      'utf8'
    );
    // Without webviewTag the <webview> guest never loads; with it,
    // the guest exists but stays a separate WebContents. Both
    // BrowserWindows load the same renderer bundle, so both need it.
    const occurrences = mainSrc.match(/webviewTag:\s*true/g) || [];
    expect(occurrences.length).toBeGreaterThanOrEqual(2);
  });
});
