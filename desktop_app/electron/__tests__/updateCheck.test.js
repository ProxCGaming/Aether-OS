/** Update check (15 Nov 2026): version comparison, the release-URL
 * boundary validation, and the never-throws check itself, with the
 * GitHub fetch stubbed — no network in tests. */
const {
  RELEASES_PAGE_URL,
  parseVersion,
  compareVersions,
  validateReleaseUrl,
  checkForUpdates,
} = require('../updateCheck.cjs');

describe('parseVersion / compareVersions', () => {
  it('parses plain and v-prefixed versions', () => {
    expect(parseVersion('0.3.5')).toEqual([0, 3, 5]);
    expect(parseVersion('v1.2')).toEqual([1, 2, 0]);
    expect(parseVersion('1.2.3-beta.1')).toEqual([1, 2, 3]);
  });

  it('refuses non-versions', () => {
    for (const bad of ['', 'latest', 'v', '1.x.0', null, undefined, 42, {}]) {
      expect(parseVersion(bad)).toBeNull();
    }
  });

  it('orders versions numerically, not lexically', () => {
    expect(compareVersions('0.3.5', '0.3.5')).toBe(0);
    expect(compareVersions('0.4.0', '0.3.5')).toBe(1);
    expect(compareVersions('0.3.5', '0.4.0')).toBe(-1);
    expect(compareVersions('0.10.0', '0.9.9')).toBe(1);
    expect(compareVersions('v2.0.0', '1.9.9')).toBe(1);
    expect(compareVersions('nope', '1.0.0')).toBeNull();
  });
});

describe('validateReleaseUrl', () => {
  it('accepts this repo\'s release URLs on github.com over https', () => {
    expect(validateReleaseUrl(RELEASES_PAGE_URL).ok).toBe(true);
    expect(validateReleaseUrl('https://github.com/ProxCGaming/Aether-OS/releases/tag/v0.4.0').ok).toBe(true);
  });

  it('refuses everything else before openExternal', () => {
    for (const bad of [
      'http://github.com/ProxCGaming/Aether-OS/releases/latest',
      'https://evil.example.com/ProxCGaming/Aether-OS/releases/latest',
      'https://github.com/SomeoneElse/Other/releases/latest',
      'https://github.com/ProxCGaming/Aether-OS',
      'file:///etc/passwd',
      '',
      null,
      42,
    ]) {
      expect(validateReleaseUrl(bad).ok).toBe(false);
    }
  });
});

describe('checkForUpdates', () => {
  const release = (tagName, extra = {}) => async () => ({ tag_name: tagName, html_url: RELEASES_PAGE_URL, ...extra });

  it('reports an available update with the release URL', async () => {
    const result = await checkForUpdates({ currentVersion: '0.3.5', fetchJson: release('v0.4.0') });
    expect(result.status).toBe('update-available');
    expect(result.latestVersion).toBe('0.4.0');
    expect(result.releaseUrl).toBe(RELEASES_PAGE_URL);
  });

  it('reports up-to-date for the same or an older tag', async () => {
    expect((await checkForUpdates({ currentVersion: '0.3.5', fetchJson: release('v0.3.5') })).status).toBe('up-to-date');
    expect((await checkForUpdates({ currentVersion: '0.3.5', fetchJson: release('v0.3.0') })).status).toBe('up-to-date');
  });

  it('never throws: a failed fetch is an error result', async () => {
    const result = await checkForUpdates({
      currentVersion: '0.3.5',
      fetchJson: async () => { throw new Error('socket hang up'); },
    });
    expect(result.status).toBe('error');
    expect(result.message).toContain('socket hang up');
    expect(result.releaseUrl).toBe(RELEASES_PAGE_URL);
  });

  it('never throws: a malformed payload is an error result', async () => {
    const result = await checkForUpdates({ currentVersion: '0.3.5', fetchJson: async () => ({ nope: true }) });
    expect(result.status).toBe('error');
  });

  it('falls back to the releases page when html_url is not a valid release URL', async () => {
    const result = await checkForUpdates({
      currentVersion: '0.3.5',
      fetchJson: release('v0.4.0', { html_url: 'https://evil.example.com/x' }),
    });
    expect(result.status).toBe('update-available');
    expect(result.releaseUrl).toBe(RELEASES_PAGE_URL);
  });
});
