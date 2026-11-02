// Build a file:// URL from an artifact's real file_path (FUP-2).
//
// The artifact store produces absolute OS paths (Python
// `str(Path)`): POSIX '/home/user/.aether/artifacts/.../index.html'
// on Linux/macOS, 'C:\Users\...\index.html' (backslashes, drive
// letter) on Windows, occasionally a UNC '\\server\share\...'.
//
// Each segment is percent-encoded (spaces, '#', '?' in file names
// must not corrupt the URL) while separators — and the Windows
// drive colon — are preserved. Returns null for anything that is
// not a usable non-empty path string, so callers can fall back.
export const toFileUrl = (filePath) => {
  if (typeof filePath !== 'string') return null;
  const raw = filePath.trim();
  if (!raw) return null;
  const encodeSegments = (segments) => segments.map((seg) => encodeURIComponent(seg));
  // UNC: \\server\share\dir\file.html -> file://server/share/dir/file.html
  if (/^\\\\/.test(raw)) {
    const segments = raw.replace(/\\/g, '/').split('/').filter(Boolean);
    if (segments.length === 0) return null;
    const [host, ...rest] = segments;
    return `file://${encodeURIComponent(host)}/${encodeSegments(rest).join('/')}`;
  }
  const normalized = raw.replace(/\\/g, '/');
  const segments = normalized.split('/').filter(Boolean);
  if (segments.length === 0) return null;
  // Windows drive: C:/dir/file.html -> file:///C:/dir/file.html
  if (/^[A-Za-z]:$/.test(segments[0])) {
    const [drive, ...rest] = segments;
    return `file:///${drive}/${encodeSegments(rest).join('/')}`;
  }
  // POSIX absolute (the store's normal shape): /dir/file.html ->
  // file:///dir/file.html. A relative path is not expected from
  // the store; treat it the same rather than dropping the preview.
  return `file:///${encodeSegments(segments).join('/')}`;
};

export default toFileUrl;
