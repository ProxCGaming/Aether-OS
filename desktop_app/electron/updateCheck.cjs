'use strict';

// Update check for the installed app (15 Nov 2026).
//
// What this is: the Settings → About "Check for updates" seam. It
// asks GitHub for the latest Release of ProxCGaming/Aether-OS and
// compares its tag with the running version (the desktop manifest's
// version, the APP_VERSION mirror).
//
// What updating MEANS here, honestly: there is no silent in-place
// updater and no per-library patching. A new version is a new
// installer, and running it replaces the WHOLE app — shell, engine
// and every bundled library — with the exact pinned set that
// release was built from (see docs/dependencies.md). User data
// (~/.aether, the app's userData) is not part of the install tree
// and is kept. This module only checks and reports; the download
// itself happens in the user's browser via the validated
// 'open-release-page' IPC in main.cjs.
//
// The check never throws: offline, a GitHub outage, or an
// unexpected payload all come back as { status: 'error', ... } so
// the UI can say so plainly instead of spinning forever.

const https = require('https');

const RELEASES_API_URL =
  'https://api.github.com/repos/ProxCGaming/Aether-OS/releases/latest';
const RELEASES_PAGE_URL =
  'https://github.com/ProxCGaming/Aether-OS/releases/latest';
const FETCH_TIMEOUT_MS = 10000;
const MAX_RESPONSE_BYTES = 1024 * 1024;

// Parse "v1.2.3" / "1.2.3" / "1.2" into [1, 2, 3]; anything else
// (pre-release suffixes are ignored after the numeric core) that
// does not start with digits and dots returns null.
function parseVersion(version) {
  if (typeof version !== 'string') return null;
  const core = version.trim().replace(/^v/i, '').split(/[-+]/)[0];
  if (!/^\d+(\.\d+)*$/.test(core)) return null;
  const parts = core.split('.').map((n) => Number(n));
  while (parts.length < 3) parts.push(0);
  return parts;
}

// -1 / 0 / 1, or null when either side is unparsable.
function compareVersions(a, b) {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  if (!pa || !pb) return null;
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const diff = (pa[i] || 0) - (pb[i] || 0);
    if (diff !== 0) return diff < 0 ? -1 : 1;
  }
  return 0;
}

// The only URLs the shell may open externally for an update: https
// on github.com, inside this repo's releases tree. Anything else
// (another host, another repo, plain http, a non-string) is refused
// before shell.openExternal is reached.
function validateReleaseUrl(url) {
  if (typeof url !== 'string' || url.length === 0) {
    return { ok: false, error: 'Refused: the release URL is missing.' };
  }
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return { ok: false, error: 'Refused: the release URL is not a URL.' };
  }
  if (parsed.protocol !== 'https:') {
    return { ok: false, error: 'Refused: the release URL is not https.' };
  }
  if (parsed.hostname !== 'github.com') {
    return { ok: false, error: 'Refused: the release URL is not on github.com.' };
  }
  if (!parsed.pathname.startsWith('/ProxCGaming/Aether-OS/releases')) {
    return { ok: false, error: 'Refused: the release URL is outside the Aether-OS releases.' };
  }
  return { ok: true, value: parsed.toString() };
}

function defaultFetchJson(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      {
        headers: {
          'User-Agent': 'Aether-OS update check',
          Accept: 'application/vnd.github+json',
        },
        timeout: FETCH_TIMEOUT_MS,
      },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          reject(new Error(`GitHub answered HTTP ${res.statusCode}`));
          return;
        }
        let size = 0;
        const chunks = [];
        res.on('data', (chunk) => {
          size += chunk.length;
          if (size > MAX_RESPONSE_BYTES) {
            req.destroy(new Error('GitHub response was unexpectedly large'));
            return;
          }
          chunks.push(chunk);
        });
        res.on('end', () => {
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
          } catch (err) {
            reject(err);
          }
        });
      },
    );
    req.on('timeout', () => req.destroy(new Error('the update check timed out')));
    req.on('error', reject);
  });
}

async function checkForUpdates({ currentVersion, fetchJson = defaultFetchJson, apiUrl = RELEASES_API_URL } = {}) {
  const base = { currentVersion, latestVersion: null, releaseUrl: RELEASES_PAGE_URL };
  let data;
  try {
    data = await fetchJson(apiUrl);
  } catch (err) {
    return {
      ...base,
      status: 'error',
      message: `Could not check for updates: ${(err && err.message) || 'no connection to GitHub.'}`,
    };
  }
  const tag = data && typeof data.tag_name === 'string' ? data.tag_name : '';
  const latestVersion = tag.replace(/^v/i, '');
  const cmp = compareVersions(latestVersion, currentVersion);
  if (cmp === null) {
    return {
      ...base,
      status: 'error',
      message: 'GitHub answered with a release whose version could not be read.',
    };
  }
  const releaseUrl =
    typeof data.html_url === 'string' && validateReleaseUrl(data.html_url).ok
      ? data.html_url
      : RELEASES_PAGE_URL;
  if (cmp > 0) {
    return {
      ...base,
      status: 'update-available',
      latestVersion,
      releaseUrl,
      message: `Version ${latestVersion} is available — you have ${currentVersion}.`,
    };
  }
  return {
    ...base,
    status: 'up-to-date',
    latestVersion,
    releaseUrl,
    message: `You're on the latest version (${currentVersion}).`,
  };
}

module.exports = {
  RELEASES_API_URL,
  RELEASES_PAGE_URL,
  parseVersion,
  compareVersions,
  validateReleaseUrl,
  checkForUpdates,
};
