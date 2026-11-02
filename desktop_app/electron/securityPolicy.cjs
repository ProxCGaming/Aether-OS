'use strict';

/**
 * S3-M2 — Electron shell hardening: CSP, permissions, navigation.
 *
 * Before this module the app had NO Content-Security-Policy, NO
 * session permission handler, NO navigation / window-open guard,
 * and its renderers ran unsandboxed (sandboxing itself is a
 * webPreferences flag in main.cjs; the preload contract that makes
 * it safe is pinned by this module's test suite). Any renderer
 * compromise or XSS therefore ran with full preload privileges and
 * could navigate the top frame anywhere.
 *
 * This module holds the pure decision logic, in the style of
 * ipcValidation.cjs / snipGeometry.cjs — no Electron import, so it
 * is unit-testable in Jest. main.cjs wires it onto:
 *   - session.defaultSession (permission request + check handlers,
 *     and, in dev, the CSP response header),
 *   - every WebContents of type 'window' via 'web-contents-created'
 *     (will-navigate guard + window-open deny),
 *   - index.html, which carries the production CSP as a <meta> tag
 *     (the built page is loaded from file://, where a response
 *     header cannot be the delivery mechanism; the test suite pins
 *     the meta content to buildContentSecurityPolicy({isDev:false})
 *     so the two can never drift).
 *
 * Trust rule: "the app's own page" means EXACTLY what S3-M3's
 * ipcValidation.cjs already decided — dev http://localhost:5173,
 * prod dist/index.html as a file:// URL, floating-window query
 * ignored. Permission and navigation decisions reuse
 * isTrustedSenderUrl rather than inventing a second notion.
 *
 * <webview> preview guests (M01): a separate session partition
 * ('aether-preview'), no preload, NOT governed by the app page's
 * CSP, and their navigation is the preview feature's purpose.
 * Nothing here touches them: the permission handlers live on the
 * default session only, and shouldGuardWebContents() attaches
 * navigation guards to type 'window' contents only.
 *
 * KNOWN CONSEQUENCE for the M01 static preview (verified with a
 * live Electron 44 probe, not assumed): a srcDoc document inherits
 * its embedder's CSP, so this policy's script-src also applies
 * inside ProjectPreview's sandboxed srcDoc iframe — inline scripts
 * in static artifact HTML no longer run there (markup and styles
 * still render; server previews in the <webview> guest are
 * unaffected, and the frame remains bridge-isolated exactly as
 * M01 built it). The alternative was worse: 'unsafe-inline' in
 * script-src to exempt the frame would hand the same exemption to
 * any injected script in the privileged app window itself. If
 * static previews need their inline scripts back, the route is to
 * serve static content through the isolated guest (which is
 * CSP-exempt), not to weaken this policy.
 */

const { isTrustedSenderUrl, DEV_ORIGIN } = require('./ipcValidation.cjs');

// ------------------------------------------------------------------
// 1. Content-Security-Policy
// ------------------------------------------------------------------

/**
 * The production policy. Every non-'self' source is tied to a
 * feature that provably needs it:
 *
 *   style-src https://fonts.googleapis.com
 *     src/index.css line 1 @imports the Inter stylesheet from
 *     Google Fonts; the @import is a style fetch.
 *   font-src https://fonts.gstatic.com
 *     The font files that stylesheet references are served from
 *     this origin. These two are the ONLY remote origins the
 *     renderer bundle uses anywhere.
 *   img-src data:
 *     Snip captures arrive as data:image/png URLs (SnipOverlay),
 *     chat attachments carry data URIs (att.data in
 *     DraggableChatWindow), and artifact images are data URIs
 *     (ArtifactsCanvas -> ImageViewer content).
 *   img-src blob:
 *     URL.createObjectURL previews/downloads (App.jsx,
 *     ArtifactsCanvas.jsx, MemoryManagement.jsx, SettingsPanel).
 *   img-src file:
 *     Attachment <img> tags are rendered straight from local
 *     paths: src={att.path ? `file://${att.path}` : …} in
 *     DraggableChatWindow (message attachments and the composer
 *     thumbnail strip).
 *   connect-src 'self' (no remote, no socket):
 *     The renderer never opens a network connection to the engine
 *     itself — SocketContext goes through window.electronAPI and
 *     the main-process WebSocket relay. Its only direct fetch is
 *     LocalModelsTab's same-origin '/ollama-catalog.json'. Voice
 *     media runs over RTCPeerConnection with engine-supplied ICE
 *     servers (VoiceOverlay.jsx); signalling rides the same IPC
 *     relay, and peer connections are not a connect-src fetch.
 *   frame-src 'self':
 *     The only frame the app page embeds is ProjectPreview's
 *     srcDoc iframe. <webview> guests are separate WebContents and
 *     are not framed documents under this policy (probe-verified).
 *   style-src carries 'unsafe-inline' — for STYLES ONLY, and only
 *     because the built bundle genuinely requires it (probe-
 *     verified on Electron 44 against the real dist build):
 *     react-force-graph-3d injects the graph chrome styles
 *     (.float-tooltip-kap / .scene-nav-info / .graph-info-msg) as
 *     <style> elements at startup, and mermaid + cytoscape inject
 *     further <style> elements with per-render, dynamically
 *     generated contents — so CSP hashes cannot cover them and a
 *     strict style-src breaks the home graph view and diagram
 *     rendering. React's own style props go through the CSSOM and
 *     never needed this. Inline style is a UI-redress vector, not
 *     a script-execution one; script-src below stays strict. (The
 *     one app-owned runtime <style> block, VoiceOverlay's waveform
 *     keyframes, was still moved into src/index.css — the
 *     exemption exists for library internals, not app code.)
 *   script-src has NO 'unsafe-inline' / 'unsafe-eval': the Vite
 *     production bundle is plain external module files, and a live
 *     probe confirms an injected inline <script> on the built page
 *     is refused under this policy.
 *
 * The dev policy relaxes ONLY what the Vite dev server genuinely
 * requires (React-refresh inline preamble, eval-based sourcemaps,
 * <style>-injected CSS, the HMR WebSocket on the dev origin) and
 * is delivered as a response header by main.cjs — never shipped.
 */
function buildContentSecurityPolicy({ isDev = false, devOrigin = DEV_ORIGIN } = {}) {
  const directives = [
    ['default-src', ["'self'"]],
    ['script-src', isDev ? ["'self'", "'unsafe-inline'", "'unsafe-eval'"] : ["'self'"]],
    ['style-src', ["'self'", "'unsafe-inline'", 'https://fonts.googleapis.com']],
    ['font-src', ["'self'", 'https://fonts.gstatic.com']],
    ['img-src', ["'self'", 'data:', 'blob:', 'file:']],
    ['connect-src', isDev ? ["'self'", devOrigin.replace(/^http/, 'ws')] : ["'self'"]],
    ['frame-src', ["'self'"]],
    ['object-src', ["'none'"]],
    ['base-uri', ["'self'"]],
    ['form-action', ["'self'"]],
  ];
  return directives.map(([name, sources]) => `${name} ${sources.join(' ')}`).join('; ');
}

/** Return a copy of `responseHeaders` with the CSP header set. */
function withCspResponseHeaders(responseHeaders, csp) {
  return {
    ...(responseHeaders && typeof responseHeaders === 'object' ? responseHeaders : {}),
    'Content-Security-Policy': [csp],
  };
}

// ------------------------------------------------------------------
// 2. Permissions — deny by default
// ------------------------------------------------------------------

/**
 * The complete allow-set, each tied to its renderer call site:
 *   'media' — navigator.mediaDevices.getUserMedia({ audio: true })
 *     in VoiceOverlay.jsx (voice overlay capture) and
 *     DraggableChatWindow.jsx (push-to-talk dictation).
 *   'clipboard-read' — navigator.clipboard.readText() in
 *     DraggableChatWindow.jsx (the paste action).
 *   'clipboard-sanitized-write' — navigator.clipboard.writeText()
 *     in DraggableChatWindow.jsx (copy/cut) and
 *     ArtifactsCanvas.jsx (copy artifact).
 * Everything else the platform offers — geolocation, notifications
 * (the app notifies from the MAIN process via Electron's
 * Notification module, which needs no renderer permission),
 * display-capture (screen capture likewise runs in main via
 * desktopCapturer), fullscreen, pointer lock, MIDI, … — is denied.
 * A permission is granted only from the app's own page (the S3-M3
 * trust rule): a justified permission from any other origin is
 * still a refusal.
 */
const ALLOWED_PERMISSIONS = Object.freeze([
  'media',
  'clipboard-read',
  'clipboard-sanitized-write',
]);
const ALLOWED_PERMISSION_SET = new Set(ALLOWED_PERMISSIONS);

function isPermissionAllowed(permission, urlString, options) {
  if (typeof permission !== 'string' || !ALLOWED_PERMISSION_SET.has(permission)) {
    return false;
  }
  return isTrustedSenderUrl(urlString, options);
}

function getWebContentsUrl(webContents) {
  if (!webContents) return '';
  if (typeof webContents.getURL === 'function') {
    try {
      const url = webContents.getURL();
      if (typeof url === 'string' && url) return url;
    } catch { /* fall through */ }
  }
  if (typeof webContents.url === 'string' && webContents.url) return webContents.url;
  return '';
}

function getRequestingUrl(webContents, details) {
  if (details && typeof details.requestingUrl === 'string' && details.requestingUrl) {
    return details.requestingUrl;
  }
  return getWebContentsUrl(webContents);
}

/**
 * Build a session.setPermissionRequestHandler callback:
 * (webContents, permission, callback, details) in Electron's
 * shape. `log` is an optional refusal logger (main.cjs passes
 * console.warn); it never affects the decision.
 */
function createPermissionRequestHandler(options, log) {
  return (webContents, permission, callback, details) => {
    const url = getRequestingUrl(webContents, details);
    const allowed = isPermissionAllowed(permission, url, options);
    if (!allowed && typeof log === 'function') {
      log(`[security] Denied permission '${permission}' for ${url || '(unknown origin)'}`);
    }
    if (typeof callback === 'function') callback(allowed);
  };
}

/**
 * Build a session.setPermissionCheckHandler callback:
 * (webContents, permission, requestingOrigin, details) -> boolean.
 * Mirrors the request handler so a capability cannot be checked
 * as available and then used without ever being requested.
 */
function createPermissionCheckHandler(options) {
  return (webContents, permission, _requestingOrigin, details) => {
    return isPermissionAllowed(permission, getRequestingUrl(webContents, details), options);
  };
}

// ------------------------------------------------------------------
// 3. Navigation + window-open guards
// ------------------------------------------------------------------

/**
 * A top-level navigation is allowed only to the app's own page —
 * the same S3-M3 trust rule (dev origin at '/' or '/index.html',
 * prod the exact dist/index.html file URL; query/hash ignored so
 * the floating window's own URL passes).
 */
function isNavigationAllowed(urlString, options) {
  return isTrustedSenderUrl(urlString, options);
}

/**
 * 'will-navigate' adapter in Electron's handler shape: returns
 * true and leaves the event alone for the app's own page; for
 * anything else it cancels the navigation (the event's default
 * action is prevented here, inside the unit-tested adapter, so
 * main.cjs stays a thin wiring line) and returns false so the
 * caller can log the refusal.
 */
function guardWillNavigate(event, urlString, options) {
  if (isNavigationAllowed(urlString, options)) return true;
  if (event && typeof event.preventDefault === 'function') {
    event.preventDefault();
  }
  return false;
}

/**
 * Window-open decision: always deny. The renderer has no
 * window.open / target=_blank call site and no legitimate popup
 * flow — links inside chat markdown are inert here rather than
 * spawning unguarded windows, and opening a local file in the OS
 * already goes through the S3-M3-guarded 'open-path' IPC
 * (shell.openPath in main.cjs).
 */
function windowOpenDecision() {
  return { action: 'deny' };
}

/**
 * Which WebContents get the navigation/window-open guards: only
 * the app's own top-level windows (getType() === 'window').
 * <webview> guests ('webview') are the M01 preview surface —
 * navigating them to arbitrary URLs is the feature — and devtools
 * / background contents are not app pages.
 */
function shouldGuardWebContents(webContents) {
  if (!webContents || typeof webContents.getType !== 'function') return false;
  try {
    return webContents.getType() === 'window';
  } catch {
    return false;
  }
}

module.exports = {
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
};
