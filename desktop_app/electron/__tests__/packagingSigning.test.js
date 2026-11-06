/**
 * Unsigned Windows builds skip the per-file signing pass
 * (owner-approved, 5 Nov 2026).
 *
 * electron-builder walks every executable it packages through its
 * signing stage — including the hundreds of .exe/.dll files in the
 * bundled Python engine payload (extraResources). The builds are
 * unsigned (no certificate is configured anywhere: no CSC_LINK in
 * CI, none locally), so the pass applies nothing and only costs
 * minutes — the friend's 3 Nov build log showed signtool grinding
 * through site-packages file by file.
 *
 * `win.signExecutable: false` is electron-builder's own switch for
 * exactly this (app-builder-lib winPackager: the sign call
 * short-circuits, the extra-files signing transformer is never
 * created, the app.asar.unpacked walk and the NSIS uninstaller
 * signing are skipped) while `signAndEditExecutable` stays enabled,
 * so the main executable still gets its icon and version metadata.
 *
 * WHEN A CERTIFICATE ARRIVES: set signExecutable back to true (or
 * remove the key) in the same change that wires CSC_LINK — with the
 * key left false, a "signed" release would silently ship unsigned.
 * The release workflow carries the same warning on its Windows job.
 */
const pkg = require('../../package.json');

describe('packaging signing configuration (unsigned builds)', () => {
  it('skips the per-file signing pass on Windows', () => {
    expect(pkg.build.win.signExecutable).toBe(false);
  });

  it('keeps executable resource editing (icon + version metadata)', () => {
    expect(pkg.build.win.signAndEditExecutable).not.toBe(false);
  });

  it('still targets the NSIS installer', () => {
    expect(pkg.build.win.target).toBe('nsis');
  });
});
