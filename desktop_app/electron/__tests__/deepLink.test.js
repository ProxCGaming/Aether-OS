/**
 * F-13 (EGM supplementary Blocker) — `aether://` deep links.
 *
 * INVESTIGATION OUTCOME (verified against base 5ee6fcc, and
 * re-checked at 4c597b7): the claim does NOT reproduce. There is
 * no deep-link surface anywhere in desktop_app:
 *
 *  - `git grep "aether://"` over the whole repo: zero hits.
 *  - No `setAsDefaultProtocolClient`, no `protocol.register*` /
 *    `protocol.handle`, no `open-url` handler, no `protocols`
 *    entry in the electron-builder config (package.json).
 *  - No `argv` / `process.argv` anywhere in desktop_app: the one
 *    OS-entry seam, the `second-instance` handler in main.cjs,
 *    takes no arguments and only shows/restores/focuses the
 *    window. Launch arguments — including any `aether://` URL the
 *    OS might deliver in them — are never read, parsed or
 *    dispatched, so no crafted URL can reach any IPC channel or
 *    the engine. (This matches the programme's pattern for
 *    non-reproducing supplementary claims: close by evidence,
 *    with a regression pin.)
 *
 * These tests PIN that state, following the static source-level
 * pattern of src/__tests__/preload-no-token.test.js. They fail by
 * design if anyone adds deep-link handling: adding it safely
 * means replacing this file, in the same change, with validation
 * tests proving only whitelisted actions with schema-validated
 * arguments can be dispatched at the main-process boundary (the
 * F-13 fix direction in the blocker-fix programme).
 */
const fs = require('fs');
const path = require('path');

const appRoot = path.join(__dirname, '..', '..');
const mainSrc = fs.readFileSync(path.join(appRoot, 'main.cjs'), 'utf8');
const preloadSrc = fs.readFileSync(path.join(appRoot, 'preload.cjs'), 'utf8');
const packageJson = JSON.parse(fs.readFileSync(path.join(appRoot, 'package.json'), 'utf8'));

function readRendererSources() {
  const srcRoot = path.join(appRoot, 'src');
  const out = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== '__tests__') walk(full);
      } else if (/\.(js|jsx)$/.test(entry.name) && !/\.test\.(js|jsx)$/.test(entry.name)) {
        out.push(fs.readFileSync(full, 'utf8'));
      }
    }
  };
  walk(srcRoot);
  return out.join('\n');
}

describe('F-13 pin: no protocol/deep-link surface is registered', () => {
  it('main registers no custom protocol and no open-url handler', () => {
    expect(mainSrc).not.toContain('setAsDefaultProtocolClient');
    expect(mainSrc).not.toMatch(/protocol\.(register|handle)/);
    expect(mainSrc).not.toContain('registerSchemesAsPrivileged');
    expect(mainSrc).not.toContain('open-url');
    expect(mainSrc).not.toContain('aether://');
  });

  it('the electron-builder config declares no protocols', () => {
    expect(packageJson.build.protocols).toBeUndefined();
  });

  it('preload exposes no deep-link channel', () => {
    expect(preloadSrc).not.toContain('deep-link');
    expect(preloadSrc).not.toContain('onDeepLink');
    expect(preloadSrc).not.toContain('aether://');
  });

  it('no electron helper module handles deep links', () => {
    const helperDir = path.join(appRoot, 'electron');
    for (const name of fs.readdirSync(helperDir)) {
      if (!name.endsWith('.cjs')) continue;
      const src = fs.readFileSync(path.join(helperDir, name), 'utf8');
      expect(src).not.toContain('aether://');
      expect(src).not.toContain('setAsDefaultProtocolClient');
      expect(src).not.toContain('open-url');
    }
  });

  it('the renderer has no deep-link handling', () => {
    const rendererSrc = readRendererSources();
    expect(rendererSrc).not.toContain('aether://');
    expect(rendererSrc).not.toContain('onDeepLink');
    expect(rendererSrc).not.toContain('deep-link');
  });
});

describe('F-13 pin: launch arguments cannot trigger any action', () => {
  it('main never reads process.argv', () => {
    expect(mainSrc).not.toContain('process.argv');
  });

  it('the second-instance handler ignores argv and dispatches nothing', () => {
    const start = mainSrc.indexOf("app.on('second-instance'");
    expect(start).toBeGreaterThanOrEqual(0);
    const block = mainSrc.slice(start, mainSrc.indexOf('});', start) + 3);
    // No argument is accepted, no URL is parsed, and nothing is
    // sent on any IPC or engine channel: the handler may only
    // show/restore/focus the existing window.
    expect(block).not.toContain('argv');
    expect(block).not.toContain('URL');
    expect(block).not.toContain('commandLine');
    expect(block).not.toContain('.send(');
    expect(block).not.toContain('ipc');
    expect(block).not.toContain('engine');
    expect(block).toContain('focus()');
  });
});
