/**
 * S3-M2 — Electron shell hardening: CSP, permission handler,
 * navigation/window-open guards, renderer sandbox.
 *
 * Before this fix the app had NO Content-Security-Policy, NO
 * session permission handler, NO navigation or window-open guard,
 * and renderers ran unsandboxed. Any renderer compromise (or XSS)
 * therefore ran with full preload privileges and could navigate
 * the top frame anywhere.
 *
 * The decision logic lives in electron/securityPolicy.cjs (pure,
 * like ipcValidation.cjs / snipGeometry.cjs); main.cjs wires it
 * onto the session and every app window. main.cjs cannot be
 * required in Jest (single-instance lock, windows, socket), so
 * the wiring is pinned by source-contract tests at the bottom —
 * the same pattern as ipcValidation.test.js / shortcutsQuit.test.js.
 *
 * Two facts about this codebase that shaped the policy (verified
 * in the code, and for the frame behaviour with a live Electron
 * probe on Electron 44):
 *
 *  - The renderer NEVER talks to the engine directly: SocketContext
 *    goes through window.electronAPI (main-process WebSocket relay).
 *    Its only direct fetch is LocalModelsTab's same-origin
 *    '/ollama-catalog.json'. So connect-src 'self' suffices.
 *  - <webview> preview guests (M01, partition 'aether-preview',
 *    separate session, no preload) are NOT governed by the app
 *    page's CSP and must NOT be navigation-guarded — previewing
 *    arbitrary URLs is their purpose. Guards attach to
 *    WebContents of type 'window' only.
 */
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const {
  ALLOWED_PERMISSIONS,
  buildContentSecurityPolicy,
  withCspResponseHeaders,
  isPermissionAllowed,
  createPermissionRequestHandler,
  createPermissionCheckHandler,
  isNavigationAllowed,
  guardWillNavigate,
  windowOpenDecision,
  shouldGuardWebContents,
} = require('../securityPolicy.cjs');

const APP_INDEX = path.join(__dirname, '..', '..', 'dist', 'index.html');
const APP_FILE_URL = pathToFileURL(APP_INDEX).href;
const PROD = { isDev: false, appIndexPath: APP_INDEX };
const DEV = { isDev: true, appIndexPath: APP_INDEX };

function directives(csp) {
  const map = {};
  for (const part of csp.split(';')) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    const [name, ...sources] = trimmed.split(/\s+/);
    map[name] = sources;
  }
  return map;
}

// ------------------------------------------------------------------
// 1. Content-Security-Policy
// ------------------------------------------------------------------
describe('S3-M2 CSP: production policy', () => {
  const csp = buildContentSecurityPolicy({ isDev: false });
  const map = directives(csp);

  it('defaults to self and locks scripts to self — no inline, no eval, no wildcards', () => {
    expect(map['default-src']).toEqual(["'self'"]);
    // The script policy is the hard line: no 'unsafe-inline', no
    // 'unsafe-eval', no remote scripts, no wildcard — anywhere.
    expect(map['script-src']).toEqual(["'self'"]);
    expect(csp).not.toContain("'unsafe-eval'");
    expect(csp).not.toContain('*');
    for (const [name, sources] of Object.entries(map)) {
      if (name !== 'style-src') expect(sources).not.toContain("'unsafe-inline'");
    }
  });

  it('style-src carries unsafe-inline for styles ONLY — bundled libraries inject <style> elements', () => {
    // Probe-verified against the real dist build (Electron 44):
    // react-force-graph-3d injects the graph chrome styles
    // (.float-tooltip-kap / .scene-nav-info / .graph-info-msg) as
    // <style> elements at startup; mermaid and cytoscape inject
    // more, with per-render dynamic contents, so hashes cannot
    // cover them. Without this, the home graph view and diagram
    // rendering break. It grants inline STYLE, never script.
    expect(map['style-src']).toEqual(["'self'", "'unsafe-inline'", 'https://fonts.googleapis.com']);
  });

  it('allows exactly the remote origins the app provably uses (Google Fonts)', () => {
    // src/index.css line 1: @import url('https://fonts.googleapis.com/…')
    // loads the stylesheet; the font files themselves are served
    // from fonts.gstatic.com. No other remote origin is used by
    // the renderer bundle.
    expect(map['font-src']).toEqual(["'self'", 'https://fonts.gstatic.com']);
    const remote = Object.values(map).flat().filter((s) => /^https?:/.test(s));
    expect(remote.sort()).toEqual(['https://fonts.googleapis.com', 'https://fonts.gstatic.com']);
  });

  it('allows the image sources the UI provably renders', () => {
    // data: — snip captures (SnipOverlay) + attachment data URIs +
    //   artifact images (ArtifactsCanvas/ImageViewer content);
    // blob: — URL.createObjectURL downloads/previews (App.jsx,
    //   ArtifactsCanvas.jsx, MemoryManagement.jsx, SettingsPanel);
    // file: — attachment <img src={`file://${att.path}`}> in
    //   DraggableChatWindow (message attachments + composer thumbs).
    expect(map['img-src']).toEqual(["'self'", 'data:', 'blob:', 'file:']);
  });

  it('connect-src is self only — the engine is reached via the main-process relay', () => {
    expect(map['connect-src']).toEqual(["'self'"]);
  });

  it('frames are self only (the srcDoc preview); objects are off; base/form are pinned', () => {
    expect(map['frame-src']).toEqual(["'self'"]);
    expect(map['object-src']).toEqual(["'none'"]);
    expect(map['base-uri']).toEqual(["'self'"]);
    expect(map['form-action']).toEqual(["'self'"]);
  });
});

describe('S3-M2 CSP: dev policy (vite)', () => {
  const csp = buildContentSecurityPolicy({ isDev: true });
  const map = directives(csp);

  it('relaxes ONLY what vite dev genuinely needs: inline/eval scripts, injected styles, HMR socket', () => {
    // Vite dev injects the React-refresh preamble as an inline
    // script, compiles modules with eval-based sourcemaps, injects
    // CSS through <style> elements, and hot-reloads over a
    // WebSocket on the dev origin. Production never sees this.
    expect(map['script-src']).toEqual(["'self'", "'unsafe-inline'", "'unsafe-eval'"]);
    expect(map['style-src']).toContain("'unsafe-inline'");
    expect(map['connect-src']).toEqual(["'self'", 'ws://localhost:5173']);
  });

  it('keeps every other directive as strict as production', () => {
    const prod = directives(buildContentSecurityPolicy({ isDev: false }));
    for (const name of ['default-src', 'font-src', 'img-src', 'frame-src', 'object-src', 'base-uri', 'form-action']) {
      expect(map[name]).toEqual(prod[name]);
    }
    expect(csp).not.toContain('*');
  });
});

describe('S3-M2 CSP: dev header application', () => {
  it('withCspResponseHeaders sets the policy without dropping other headers', () => {
    const out = withCspResponseHeaders({ 'Content-Type': ['text/html'] }, 'default-src \'self\'');
    expect(out['Content-Type']).toEqual(['text/html']);
    expect(out['Content-Security-Policy']).toEqual(["default-src 'self'"]);
    expect(withCspResponseHeaders(undefined, 'x')).toEqual({ 'Content-Security-Policy': ['x'] });
  });
});

// ------------------------------------------------------------------
// 2. Permission decisions — deny by default
// ------------------------------------------------------------------
describe('S3-M2 permissions', () => {
  it('the allow-set is exactly the permissions the renderer provably requests', () => {
    // media — getUserMedia({audio:true}): VoiceOverlay.jsx (voice
    //   overlay), DraggableChatWindow.jsx (push-to-talk dictation).
    // clipboard-read — navigator.clipboard.readText():
    //   DraggableChatWindow.jsx (paste action).
    // clipboard-sanitized-write — navigator.clipboard.writeText():
    //   DraggableChatWindow.jsx, ArtifactsCanvas.jsx.
    expect([...ALLOWED_PERMISSIONS].sort()).toEqual([
      'clipboard-read',
      'clipboard-sanitized-write',
      'media',
    ]);
  });

  it('allows the justified set from the app\'s own pages (dev + prod + floating)', () => {
    for (const permission of ALLOWED_PERMISSIONS) {
      expect(isPermissionAllowed(permission, APP_FILE_URL, PROD)).toBe(true);
      expect(isPermissionAllowed(permission, `${APP_FILE_URL}?floating=true&session=abc`, PROD)).toBe(true);
      expect(isPermissionAllowed(permission, 'http://localhost:5173/', DEV)).toBe(true);
      expect(isPermissionAllowed(permission, 'http://localhost:5173?floating=true&session=abc', DEV)).toBe(true);
    }
  });

  it('denies everything else, even from the app\'s own page', () => {
    for (const permission of [
      'geolocation', 'notifications', 'display-capture', 'fullscreen',
      'pointerLock', 'keyboardLock', 'midi', 'midiSysex', 'mediaKeySystem',
      'idle-detection', 'window-management', 'storage-access',
      'openExternal', 'fileSystem', 'unknown', '', undefined, null,
    ]) {
      expect(isPermissionAllowed(permission, APP_FILE_URL, PROD)).toBe(false);
      expect(isPermissionAllowed(permission, 'http://localhost:5173/', DEV)).toBe(false);
    }
  });

  it('denies even justified permissions from any other origin', () => {
    for (const url of [
      'https://evil.example.com/phish',
      'http://localhost:3000/preview',
      'data:text/html,<script>alert(1)</script>',
      '',
      undefined,
    ]) {
      expect(isPermissionAllowed('media', url, PROD)).toBe(false);
      expect(isPermissionAllowed('media', url, DEV)).toBe(false);
    }
  });

  it('the request handler answers through the callback and logs refusals', () => {
    const logs = [];
    const handler = createPermissionRequestHandler(PROD, (msg) => logs.push(msg));
    const ownContents = { getURL: () => APP_FILE_URL };
    const cb = jest.fn();
    handler(ownContents, 'media', cb, { requestingUrl: APP_FILE_URL });
    expect(cb).toHaveBeenCalledWith(true);
    cb.mockClear();
    handler(ownContents, 'geolocation', cb, { requestingUrl: APP_FILE_URL });
    expect(cb).toHaveBeenCalledWith(false);
    expect(logs.some((m) => m.includes('geolocation'))).toBe(true);
    cb.mockClear();
    // No requestingUrl in details: fall back to the WebContents URL.
    handler({ getURL: () => 'https://evil.example.com/' }, 'media', cb, {});
    expect(cb).toHaveBeenCalledWith(false);
  });

  it('the check handler mirrors the request handler', () => {
    const check = createPermissionCheckHandler(PROD);
    const ownContents = { getURL: () => APP_FILE_URL };
    expect(check(ownContents, 'media', 'file://', { requestingUrl: APP_FILE_URL })).toBe(true);
    expect(check(ownContents, 'clipboard-read', 'file://', {})).toBe(true);
    expect(check(ownContents, 'geolocation', 'file://', {})).toBe(false);
    expect(check({ getURL: () => 'https://evil.example.com/' }, 'media', 'https://evil.example.com', {})).toBe(false);
  });
});

// ------------------------------------------------------------------
// 3. Navigation + window-open guards
// ------------------------------------------------------------------
describe('S3-M2 navigation guard', () => {
  it('allows the app\'s own pages (dev, prod, floating query)', () => {
    expect(isNavigationAllowed(APP_FILE_URL, PROD)).toBe(true);
    expect(isNavigationAllowed(`${APP_FILE_URL}?floating=true&session=abc`, PROD)).toBe(true);
    expect(isNavigationAllowed('http://localhost:5173', DEV)).toBe(true);
    expect(isNavigationAllowed('http://localhost:5173/', DEV)).toBe(true);
    expect(isNavigationAllowed('http://localhost:5173/index.html', DEV)).toBe(true);
    expect(isNavigationAllowed('http://localhost:5173?floating=true&session=abc', DEV)).toBe(true);
  });

  it('denies navigation anywhere else', () => {
    for (const url of [
      'https://evil.example.com/',
      'http://localhost:3000/preview',
      'http://127.0.0.1:5173/',
      'data:text/html,<script>alert(1)</script>',
      'about:blank',
      'javascript:alert(1)',
      '',
      undefined,
    ]) {
      expect(isNavigationAllowed(url, PROD)).toBe(false);
      expect(isNavigationAllowed(url, DEV)).toBe(false);
    }
    // Other local files are not the app page.
    expect(isNavigationAllowed(pathToFileURL('/home/user/evil.html').href, PROD)).toBe(false);
    expect(isNavigationAllowed(pathToFileURL(path.join(__dirname, '..', '..', 'dist', 'other.html')).href, PROD)).toBe(false);
  });

  it('guardWillNavigate prevents denied navigations and lets own-page ones through', () => {
    const denied = { preventDefault: jest.fn() };
    expect(guardWillNavigate(denied, 'https://evil.example.com/', PROD)).toBe(false);
    expect(denied.preventDefault).toHaveBeenCalledTimes(1);
    const allowed = { preventDefault: jest.fn() };
    expect(guardWillNavigate(allowed, APP_FILE_URL, PROD)).toBe(true);
    expect(allowed.preventDefault).not.toHaveBeenCalled();
  });

  it('window.open / target=_blank is always denied', () => {
    // The renderer has no window.open call site and no legitimate
    // popup flow; external files already go through the guarded
    // 'open-path' IPC (shell.openPath) instead.
    expect(windowOpenDecision()).toEqual({ action: 'deny' });
    expect(windowOpenDecision('https://example.com/')).toEqual({ action: 'deny' });
    expect(windowOpenDecision(APP_FILE_URL)).toEqual({ action: 'deny' });
  });

  it('only top-level app windows are guarded — webview guests are left alone', () => {
    expect(shouldGuardWebContents({ getType: () => 'window' })).toBe(true);
    // M01 preview guests: navigation IS the feature.
    expect(shouldGuardWebContents({ getType: () => 'webview' })).toBe(false);
    expect(shouldGuardWebContents({ getType: () => 'devtools' })).toBe(false);
    expect(shouldGuardWebContents({ getType: () => 'backgroundPage' })).toBe(false);
    expect(shouldGuardWebContents({})).toBe(false);
    expect(shouldGuardWebContents(null)).toBe(false);
  });
});

// ------------------------------------------------------------------
// Source contracts: index.html, vite config, preload, VoiceOverlay
// ------------------------------------------------------------------
const appRoot = path.join(__dirname, '..', '..');
const readApp = (...parts) => fs.readFileSync(path.join(appRoot, ...parts), 'utf8');

describe('S3-M2 source contract: CSP delivery', () => {
  it('index.html carries the production policy as a meta tag, exactly as built', () => {
    const html = readApp('index.html');
    const prod = buildContentSecurityPolicy({ isDev: false });
    expect(html).toContain(`<meta http-equiv="Content-Security-Policy" content="${prod}" />`);
  });

  it('vite strips the production meta in dev (HMR takes the header policy instead)', () => {
    const viteConfig = readApp('vite.config.js');
    expect(viteConfig).toContain('transformIndexHtml');
    expect(viteConfig).toContain('Content-Security-Policy');
  });
});

describe('S3-M2 source contract: VoiceOverlay styles moved to a stylesheet', () => {
  it('the voice waveform CSS lives in index.css, not a runtime <style> element', () => {
    // style-src 'unsafe-inline' exists only for bundled-library
    // <style> injection (see the CSP describe above); app-owned
    // CSS must not add to that surface, so the one app-owned
    // runtime <style> block (VoiceOverlay's keyframes) lives in
    // the app stylesheet instead.
    const overlay = readApp('src', 'components', 'VoiceOverlay.jsx');
    expect(overlay).not.toContain('<style>');
    const css = readApp('src', 'index.css');
    expect(css).toContain('@keyframes voiceWavePulse');
    expect(css).toContain('@keyframes voiceDotPulse');
    expect(css).toContain('.voice-wave-bar');
    expect(css).toContain('.voice-mic-dot');
  });
});

describe('S3-M2 source contract: sandbox compatibility', () => {
  it('preload.cjs requires nothing beyond the electron module', () => {
    // Sandboxed preloads may require('electron') only — no 'ws',
    // no 'fs', no arbitrary modules. contextBridge + ipcRenderer
    // are both in the sandbox-allowed API set.
    const preload = readApp('preload.cjs');
    const requires = [...preload.matchAll(/require\(\s*'([^']+)'\s*\)/g)].map((m) => m[1]);
    expect(requires.length).toBeGreaterThan(0);
    expect([...new Set(requires)]).toEqual(['electron']);
    expect(preload).toContain('contextBridge');
    expect(preload).toContain('ipcRenderer');
  });
});

// ------------------------------------------------------------------
// Source contract: main.cjs wiring
// ------------------------------------------------------------------
const mainSource = readApp('main.cjs');

describe('S3-M2 main.cjs wiring contract', () => {
  it('main.cjs loads the shared securityPolicy helper', () => {
    expect(mainSource).toContain("require('./electron/securityPolicy.cjs')");
    expect(mainSource).toContain('buildContentSecurityPolicy');
    expect(mainSource).toContain('createPermissionRequestHandler');
    expect(mainSource).toContain('createPermissionCheckHandler');
    expect(mainSource).toContain('guardWillNavigate');
    expect(mainSource).toContain('windowOpenDecision');
    expect(mainSource).toContain('shouldGuardWebContents');
  });

  it('installs permission handlers on the default session', () => {
    expect(mainSource).toContain('session.defaultSession.setPermissionRequestHandler(');
    expect(mainSource).toContain('session.defaultSession.setPermissionCheckHandler(');
  });

  it('serves the dev CSP as a header on the dev server origin, main-frame only', () => {
    expect(mainSource).toContain('webRequest.onHeadersReceived(');
    expect(mainSource).toContain('DEV_ORIGIN');
    expect(mainSource).toContain("'mainFrame'");
  });

  it('guards every app window via web-contents-created: will-navigate + window-open', () => {
    expect(mainSource).toContain("app.on('web-contents-created'");
    expect(mainSource).toContain("'will-navigate'");
    expect(mainSource).toContain('setWindowOpenHandler(');
    expect(mainSource).toContain('shouldGuardWebContents(contents)');
  });

  it('both windows run their renderers sandboxed', () => {
    expect(mainSource.match(/sandbox:\s*true/g)).toHaveLength(2);
  });

  it('the security policy is installed before the first window is created', () => {
    const installAt = mainSource.indexOf('installSecurityPolicy();');
    const createAt = mainSource.indexOf('createWindow();', mainSource.indexOf('app.whenReady()'));
    expect(installAt).toBeGreaterThanOrEqual(0);
    expect(createAt).toBeGreaterThan(installAt);
  });
});
