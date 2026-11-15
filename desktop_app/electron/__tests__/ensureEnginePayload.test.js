/**
 * predist guard (desktop_app/ensure-engine-payload.cjs): `npm run dist`
 * must refuse a MISSING payload (the 4 Nov 2026 no-engine installer)
 * and, since 15 Nov 2026, a STALE one — a payload assembled from a
 * different uv.lock than the repo's current lock would ship outdated
 * Python libraries (the drift behind the litellm/mcp incident). The
 * freshness proof is the assembler's `uv_lock_sha256` manifest field.
 */
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { checkEnginePayload } = require('../../ensure-engine-payload.cjs');

const LOCK_V1 = 'lock content v1';
const LOCK_V2 = 'lock content v2';
const sha = (s) => crypto.createHash('sha256').update(s).digest('hex');

let root;
let payloadDir;
let repoDir;

function writePayload({ manifest } = {}) {
  const exeRel = path.join('python', 'bin', 'python3');
  fs.mkdirSync(path.join(payloadDir, 'python', 'bin'), { recursive: true });
  fs.writeFileSync(path.join(payloadDir, exeRel), '');
  if (manifest !== undefined) {
    fs.writeFileSync(
      path.join(payloadDir, 'payload-manifest.json'),
      typeof manifest === 'string' ? manifest : JSON.stringify(manifest)
    );
  }
}

function writeLock(content) {
  fs.writeFileSync(path.join(repoDir, 'uv.lock'), content);
}

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'ensure-payload-'));
  payloadDir = path.join(root, 'engine-payload');
  repoDir = path.join(root, 'repo');
  fs.mkdirSync(payloadDir, { recursive: true });
  fs.mkdirSync(repoDir, { recursive: true });
});

afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true });
});

const check = () =>
  checkEnginePayload({ payloadDir, repoRoot: repoDir, platform: 'linux' });

test('missing interpreter -> missing', () => {
  writeLock(LOCK_V1);
  expect(check().reason).toBe('missing');
});

test('missing manifest -> missing', () => {
  writePayload({ manifest: undefined });
  writeLock(LOCK_V1);
  expect(check().reason).toBe('missing');
});

test('unreadable manifest -> manifest-unreadable', () => {
  writePayload({ manifest: '{not json' });
  writeLock(LOCK_V1);
  expect(check().reason).toBe('manifest-unreadable');
});

test('manifest without uv_lock_sha256 -> no-provenance (stale by definition)', () => {
  writePayload({ manifest: { app_version: '0.0.0' } });
  writeLock(LOCK_V1);
  expect(check().reason).toBe('no-provenance');
});

test('payload built from an older lock -> stale, naming both hashes', () => {
  writePayload({ manifest: { uv_lock_sha256: sha(LOCK_V1) } });
  writeLock(LOCK_V2);
  const result = check();
  expect(result.ok).toBe(false);
  expect(result.reason).toBe('stale');
  expect(result.payloadLockHash).toBe(sha(LOCK_V1));
  expect(result.currentLockHash).toBe(sha(LOCK_V2));
});

test('payload built from the current lock -> ok', () => {
  writePayload({ manifest: { uv_lock_sha256: sha(LOCK_V1) } });
  writeLock(LOCK_V1);
  const result = check();
  expect(result.ok).toBe(true);
  expect(result.pythonExe).toBe(path.join(payloadDir, 'python', 'bin', 'python3'));
});
