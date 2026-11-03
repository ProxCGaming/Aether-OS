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

const fs = require('fs');
const path = require('path');

const EXE_BY_PLATFORM = {
  win32: path.join('python', 'python.exe'),
  linux: path.join('python', 'bin', 'python3'),
  darwin: path.join('python', 'bin', 'python3'),
};

const rel = EXE_BY_PLATFORM[process.platform];
const payloadDir = path.join(__dirname, 'engine-payload');
const pythonExe = rel ? path.join(payloadDir, rel) : null;
const manifest = path.join(payloadDir, 'payload-manifest.json');

if (!pythonExe || !fs.existsSync(pythonExe) || !fs.existsSync(manifest)) {
  console.error('');
  console.error('ERROR: bundled engine payload is missing or incomplete.');
  console.error(`  expected interpreter: ${pythonExe || '(unknown platform)'}`);
  console.error('  expected manifest:    ' + manifest);
  console.error('');
  console.error('Assemble it first, from the REPO ROOT (needs Python 3.12 + uv on PATH):');
  console.error(`  python scripts/assemble_engine_payload.py --platform ${process.platform}`);
  console.error('');
  console.error('Packaging without the payload would ship an app with no engine.');
  process.exit(1);
}

console.log('Engine payload present:', pythonExe);
