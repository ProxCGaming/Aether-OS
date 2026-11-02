/**
 * E10-F06 — the assembler<->launcher layout contract.
 *
 * scripts/engine_payload_layout.json is the ONE place the payload
 * shape is pinned. This suite fails if any consumer drifts from it:
 * the launcher module's constants, the electron-builder
 * extraResources mapping in package.json, or the assembler's
 * presence. The Python side is pinned by
 * tests/test_engine_payload_assembler.py against the same file.
 */
const fs = require('fs');
const path = require('path');

const repoRoot = path.join(__dirname, '..', '..', '..');
const layout = JSON.parse(
  fs.readFileSync(path.join(repoRoot, 'scripts', 'engine_payload_layout.json'), 'utf8')
);
const lifecycle = require('../engineLifecycle.cjs');
const pkg = JSON.parse(
  fs.readFileSync(path.join(repoRoot, 'desktop_app', 'package.json'), 'utf8')
);

const norm = (p) => p.replace(/\\/g, '/');

describe('engine payload layout contract', () => {
  it('launcher constants match the pinned layout', () => {
    expect(lifecycle.ENGINE_PORT).toBe(layout.port);
    expect(lifecycle.HEALTH_PATH).toBe(layout.healthPath);
    expect(lifecycle.PAYLOAD_DIR_NAME).toBe(layout.payloadDirName);
    expect(lifecycle.RUNTIME_DIR).toBe(layout.runtimeDir);
    expect(lifecycle.APP_DIR).toBe(layout.appDir);
    expect(lifecycle.ENTRY_MODULE).toBe(layout.entryModule);
    expect(lifecycle.MANIFEST_FILE).toBe(layout.manifestFile);
    for (const platform of ['win32', 'linux', 'darwin']) {
      expect(norm(lifecycle.PYTHON_EXE_BY_PLATFORM[platform]))
        .toBe(norm(layout.pythonExe[platform]));
    }
  });

  it('electron-builder carries the assembled payload into resources under the layout name', () => {
    const extra = pkg.build.extraResources || [];
    const entry = extra.find((e) => e.to === layout.payloadDirName);
    expect(entry).toBeDefined();
    // The assembler writes to layout.assembledDir (repo-relative);
    // the builder's `from` is that directory's path relative to
    // desktop_app/.
    const expectedFrom = path.relative(
      path.join(repoRoot, 'desktop_app'),
      path.join(repoRoot, layout.assembledDir)
    );
    expect(norm(entry.from)).toBe(norm(expectedFrom));
  });

  it('the assembler exists and reads the same layout file', () => {
    const assemblerPath = path.join(repoRoot, 'scripts', 'assemble_engine_payload.py');
    expect(fs.existsSync(assemblerPath)).toBe(true);
    const src = fs.readFileSync(assemblerPath, 'utf8');
    expect(src).toContain('engine_payload_layout.json');
  });
});
