'use strict';

// Guard for `npm run dist` (wired as predist in package.json).
//
// The installer carries the Python engine as a payload folder
// (engine-payload/, assembled by scripts/assemble_engine_payload.py,
// copied into resources/engine via extraResources). That folder is
// gitignored, so a fresh checkout does NOT have it — and
// electron-builder will happily package an installer WITHOUT it,
// producing an app that sits at "initialising" forever because it
// has no engine to spawn (this exact failure hit Windows QA on
// 4 Nov 2026). Fail loudly here instead of shipping a shell.
//
// Presence is not enough (15 Nov 2026): a payload assembled from an
// OLD uv.lock packages outdated libraries just as happily — that
// drift is how a broken litellm/mcp pair reached an installed copy.
// The assembler records the SHA-256 of the uv.lock it installed from
// in payload-manifest.json (`uv_lock_sha256`); this guard compares
// it against the repo's current uv.lock and refuses to package a
// stale payload. Re-assembling is the only fix it accepts.

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const EXE_BY_PLATFORM = {
  win32: path.join('python', 'python.exe'),
  linux: path.join('python', 'bin', 'python3'),
  darwin: path.join('python', 'bin', 'python3'),
};

function sha256File(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

// Pure check, exported for tests. Returns { ok: true, pythonExe } or
// { ok: false, reason, ... } with reason one of: 'unknown-platform',
// 'missing', 'manifest-unreadable', 'no-provenance', 'lock-missing',
// 'stale'.
function checkEnginePayload({ payloadDir, repoRoot, platform }) {
  const rel = EXE_BY_PLATFORM[platform];
  if (!rel) return { ok: false, reason: 'unknown-platform', platform };
  const pythonExe = path.join(payloadDir, rel);
  const manifestPath = path.join(payloadDir, 'payload-manifest.json');
  if (!fs.existsSync(pythonExe) || !fs.existsSync(manifestPath)) {
    return { ok: false, reason: 'missing', pythonExe, manifestPath };
  }
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  } catch {
    return { ok: false, reason: 'manifest-unreadable', pythonExe, manifestPath };
  }
  const payloadLockHash = manifest.uv_lock_sha256;
  if (!payloadLockHash) {
    // Assembled before lock provenance existed (pre-15 Nov 2026):
    // its dependency set cannot be proven current, so it is stale
    // by definition.
    return { ok: false, reason: 'no-provenance', pythonExe, manifestPath };
  }
  const lockPath = path.join(repoRoot, 'uv.lock');
  if (!fs.existsSync(lockPath)) {
    return { ok: false, reason: 'lock-missing', pythonExe, manifestPath, lockPath };
  }
  const currentLockHash = sha256File(lockPath);
  if (payloadLockHash !== currentLockHash) {
    return {
      ok: false,
      reason: 'stale',
      pythonExe,
      manifestPath,
      lockPath,
      payloadLockHash,
      currentLockHash,
    };
  }
  return { ok: true, pythonExe };
}

function main() {
  const payloadDir = path.join(__dirname, 'engine-payload');
  const repoRoot = path.join(__dirname, '..');
  const result = checkEnginePayload({
    payloadDir,
    repoRoot,
    platform: process.platform,
  });
  if (result.ok) {
    console.log('Engine payload present and built from the current uv.lock:', result.pythonExe);
    return;
  }
  const assemble = `  python scripts/assemble_engine_payload.py --platform ${process.platform}`;
  console.error('');
  if (result.reason === 'missing') {
    console.error('ERROR: bundled engine payload is missing or incomplete.');
    console.error(`  expected interpreter: ${result.pythonExe || '(unknown platform)'}`);
    console.error('  expected manifest:    ' + result.manifestPath);
    console.error('');
    console.error('Assemble it first, from the REPO ROOT (needs Python 3.12 + uv on PATH):');
    console.error(assemble);
    console.error('');
    console.error('Packaging without the payload would ship an app with no engine.');
  } else if (result.reason === 'stale') {
    console.error('ERROR: bundled engine payload is STALE — it was assembled from a different uv.lock.');
    console.error(`  payload built from uv.lock sha256: ${result.payloadLockHash}`);
    console.error(`  repo's current uv.lock sha256:     ${result.currentLockHash}`);
    console.error('');
    console.error('Packaging it would ship outdated Python libraries. Re-assemble from the REPO ROOT:');
    console.error(assemble);
  } else if (result.reason === 'no-provenance') {
    console.error('ERROR: bundled engine payload predates lock provenance (no uv_lock_sha256 in its manifest),');
    console.error('so its dependency set cannot be proven current. Re-assemble from the REPO ROOT:');
    console.error(assemble);
  } else {
    console.error(`ERROR: bundled engine payload check failed (${result.reason}). Re-assemble from the REPO ROOT:`);
    console.error(assemble);
  }
  process.exit(1);
}

if (require.main === module) {
  main();
}

module.exports = { checkEnginePayload, EXE_BY_PLATFORM };
