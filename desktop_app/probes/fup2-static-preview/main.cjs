'use strict';
/**
 * FUP-2 live Electron probe (not shipped; run manually):
 *   xvfb-run -a desktop_app/node_modules/electron/dist/electron \
 *     --no-sandbox desktop_app/probes/fup2-static-preview/main.cjs
 * (On a machine with a display, xvfb-run is unnecessary. This
 * directory sits outside electron/ on purpose: electron-builder
 * packages electron/** into the app, and a probe must not ship.)
 *
 * Proves the mechanism FUP-2 lands on: a <webview
 * partition="aether-preview"> inside a host page carrying the app
 * window's strict production CSP (S3-M2) can load a file:// page
 * that (a) runs an inline <script>, (b) loads a relative sibling
 * script, (c) applies a relative sibling stylesheet, and (d) loads
 * a relative sibling image — and the guest has no electronAPI
 * bridge (M01 isolation).
 *
 * Prints one JSON line prefixed PROBE_RESULT and exits 0/1.
 */
const { app, BrowserWindow } = require('electron');
const path = require('path');
const { pathToFileURL } = require('url');

const fixtureUrl = pathToFileURL(path.join(__dirname, 'fixture', 'index.html')).href;
const hostUrl = pathToFileURL(path.join(__dirname, 'host.html')).href
  + '?target=' + encodeURIComponent(fixtureUrl);

let guest = null;
app.on('web-contents-created', (_event, contents) => {
  if (contents.getType() === 'webview') guest = contents;
});

const fail = (msg, extra) => {
  console.log('PROBE_RESULT ' + JSON.stringify({ ok: false, error: msg, ...(extra || {}) }));
  app.exit(1);
};

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 900,
    height: 700,
    show: false,
    webPreferences: { webviewTag: true },
  });

  const timeout = setTimeout(() => fail('timeout waiting for guest load'), 20000);

  try {
    await win.loadURL(hostUrl);
    // Wait for the guest to appear and finish loading.
    const deadline = Date.now() + 15000;
    let loaded = false;
    while (Date.now() < deadline) {
      if (guest && !guest.isLoading()) { loaded = true; break; }
      await new Promise((r) => setTimeout(r, 100));
    }
    if (!guest) return fail('no webview guest WebContents was created');
    if (!loaded) return fail('guest never finished loading', { url: guest.getURL() });
    // Let onload handlers / image settle.
    await new Promise((r) => setTimeout(r, 500));

    const result = await guest.executeJavaScript(`({
      url: location.href,
      inlineRan: window.__inlineRan || null,
      inlineMarker: (document.getElementById('inline-marker') || {}).textContent || null,
      siblingRan: window.__siblingRan || null,
      bodyBg: getComputedStyle(document.body).backgroundColor,
      imgLoaded: window.__imgLoaded === true,
      imgError: window.__imgError === true,
      hasElectronAPI: typeof window.electronAPI !== 'undefined',
    })`);

    const checks = {
      guestUrlIsFixtureFile: result.url === fixtureUrl,
      inlineScriptRan: result.inlineRan === 'inline-yes' && result.inlineMarker === 'inline-yes',
      siblingScriptRan: result.siblingRan === 'sibling-yes',
      siblingCssApplied: result.bodyBg === 'rgb(12, 34, 56)',
      siblingImageLoaded: result.imgLoaded && !result.imgError,
      noBridgeInGuest: result.hasElectronAPI === false,
    };
    const ok = Object.values(checks).every(Boolean);
    clearTimeout(timeout);
    console.log('PROBE_RESULT ' + JSON.stringify({ ok, checks, result, fixtureUrl }));
    app.exit(ok ? 0 : 1);
  } catch (err) {
    clearTimeout(timeout);
    fail(String(err && err.message || err), { guestUrl: guest ? guest.getURL() : null });
  }
});
