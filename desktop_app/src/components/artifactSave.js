// ---------------------------------------------------------------------
// N06: "Save a copy" must preserve the artifact's real bytes and
// MIME type. The engine's content surface delivers images as
// data:{mime};base64 URLs and everything else as UTF-8 text, so the
// payload is derived from what actually arrived — never re-typed as
// text/plain. Returns null when there is nothing real to save.
// ---------------------------------------------------------------------

const MIME_BY_EXTENSION = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.bmp': 'image/bmp',
  '.ico': 'image/x-icon',
  '.json': 'application/json',
  '.md': 'text/markdown',
  '.markdown': 'text/markdown',
  '.html': 'text/html',
  '.htm': 'text/html',
  '.csv': 'text/csv',
  '.pdf': 'application/pdf',
  '.txt': 'text/plain',
};

// Small dependency-free UTF-8 encoder (TextEncoder is not present
// in every renderer/test environment this component runs in).
const utf8Bytes = (text) => {
  const bytes = [];
  for (let i = 0; i < text.length; i++) {
    let code = text.charCodeAt(i);
    if (code < 0x80) {
      bytes.push(code);
    } else if (code < 0x800) {
      bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    } else if (code >= 0xd800 && code <= 0xdbff && i + 1 < text.length) {
      const next = text.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        code = 0x10000 + ((code - 0xd800) * 0x400) + (next - 0xdc00);
        i++;
        bytes.push(
          0xf0 | (code >> 18),
          0x80 | ((code >> 12) & 0x3f),
          0x80 | ((code >> 6) & 0x3f),
          0x80 | (code & 0x3f),
        );
      } else {
        bytes.push(0xef, 0xbf, 0xbd); // lone surrogate -> U+FFFD
      }
    } else {
      bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    }
  }
  return new Uint8Array(bytes);
};

const base64Bytes = (b64) => {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
};

export function artifactSavePayload(artifact, activeContent) {
  const raw = (activeContent && (activeContent.content || activeContent.diff)) || '';
  if (!raw) return null;

  let filename = artifact?.title || 'artifact.txt';
  if (artifact?.kind === 'report' && !filename.includes('.')) {
    filename = `${filename}.md`;
  }

  const dataUrl = /^data:([^;,]*)(;[^,]*)?,([\s\S]*)$/.exec(raw);
  if (dataUrl) {
    const mimeType = dataUrl[1] || 'application/octet-stream';
    let bytes;
    if ((dataUrl[2] || '').toLowerCase().includes(';base64')) {
      bytes = base64Bytes(dataUrl[3]);
    } else {
      let text = dataUrl[3];
      try { text = decodeURIComponent(text); } catch { /* keep raw text */ }
      bytes = utf8Bytes(text);
    }
    return { bytes, mimeType, filename };
  }

  const nameForMime = (artifact?.file_path || filename).toLowerCase();
  const dot = nameForMime.lastIndexOf('.');
  const ext = dot >= 0 ? nameForMime.slice(dot) : '';
  const mimeType = MIME_BY_EXTENSION[ext] || 'text/plain';
  return { bytes: utf8Bytes(raw), mimeType, filename };
}
