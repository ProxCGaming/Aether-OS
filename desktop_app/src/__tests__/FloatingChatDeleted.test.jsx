import fs from 'fs';
import path from 'path';

// Phase 10.3 C2: FloatingChat.jsx was dead but shipped — the floating window
// reuses App.jsx?floating=true, and nothing ever imported the component
// (it also carried its own direct onEngineMessage subscription, bypassing
// the single SocketContext facade). It was deleted; these tests fail if
// the file or any import of it reappears.
//
// Note: the identifiers openFloatingChat / closeFloatingChat are live IPC
// methods (preload.cjs / main.cjs) and are NOT the component — the import
// patterns below match only actual imports of the component file.

const SRC_DIR = path.join(process.cwd(), 'src');

// Matches a static, require, or dynamic import whose module path ends in
// FloatingChat, in any quoting style.
const FLOATING_CHAT_IMPORT =
  /(?:import\s+[^;]*?\sfrom\s+['"][^'"]*FloatingChat['"]|require\(\s*['"][^'"]*FloatingChat['"]\s*\)|import\(\s*['"][^'"]*FloatingChat['"]\s*\))/;

function walkJsFiles(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkJsFiles(full));
    else if (/\.jsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

describe('FloatingChat is deleted (Phase 10.3 C2)', () => {
  it('the FloatingChat.jsx file does not exist', () => {
    expect(fs.existsSync(path.join(SRC_DIR, 'FloatingChat.jsx'))).toBe(false);
  });

  it('no source file imports the FloatingChat component', () => {
    const offenders = walkJsFiles(SRC_DIR).filter((file) => {
      const source = fs.readFileSync(file, 'utf8');
      return FLOATING_CHAT_IMPORT.test(source);
    });
    expect(offenders).toEqual([]);
  });

  it('the floating window still has its live IPC route (App.jsx?floating=true, not the component)', () => {
    // Guard against "fixing" the deletion by removing the floating-window
    // feature itself: App must still open/close the floating window via IPC.
    const appSource = fs.readFileSync(path.join(SRC_DIR, 'App.jsx'), 'utf8');
    expect(appSource).toContain('openFloatingChat');
    expect(appSource).toContain('closeFloatingChat');
    expect(appSource).toContain("params.get('floating') === 'true'");
  });
});
