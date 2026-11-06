import { artifactSavePayload } from '../artifactSave';

// artifactSave — direct tests (the D3 chunk found this module had
// none). Wave 3 pins:
//   D3-06 — a malformed ;base64 data URL must not throw out of
//           "Save a copy"; the module's contract is "returns null
//           when there is nothing real to save", and undecodable
//           bytes are not real bytes.
//   D3-26 — the UTF-8 encoder must emit U+FFFD for a lone LOW
//           surrogate, exactly as it already does for a lone high
//           surrogate; emitting the surrogate code point itself is
//           invalid UTF-8 (WTF-8) in the saved file.

const artifact = {
  id: 'art-1', kind: 'file', title: 'notes.txt', file_path: '/path/notes.txt',
};

describe('D3-06 — malformed base64 never throws', () => {
  it('returns null for a data URL whose base64 payload is undecodable', () => {
    const active = {
      content: 'data:image/png;base64,!!!not-base64!!!',
      diff: null, exists: true, error: null,
    };
    let payload;
    expect(() => { payload = artifactSavePayload(artifact, active); }).not.toThrow();
    expect(payload).toBeNull();
  });

  it('still decodes a well-formed base64 data URL', () => {
    const payload = artifactSavePayload(
      { ...artifact, title: 'photo.png', file_path: '/path/photo.png' },
      { content: 'data:image/png;base64,iVBORw0KGgo=', diff: null, exists: true, error: null },
    );
    expect(payload).not.toBeNull();
    expect(payload.bytes).toHaveLength(8);
    expect(payload.bytes[0]).toBe(0x89);
  });
});

describe('D3-26 — UTF-8 encoder replaces lone surrogates symmetrically', () => {
  const bytesOf = (text) => {
    const payload = artifactSavePayload(artifact, {
      content: text, diff: null, exists: true, error: null,
    });
    return Array.from(payload.bytes);
  };

  it('encodes a lone low surrogate as U+FFFD', () => {
    // 'a' + lone low surrogate + 'b'
    expect(bytesOf('a\udc00b')).toEqual([0x61, 0xef, 0xbf, 0xbd, 0x62]);
  });

  it('encodes a lone high surrogate as U+FFFD (already held)', () => {
    expect(bytesOf('a\ud800b')).toEqual([0x61, 0xef, 0xbf, 0xbd, 0x62]);
  });

  it('still encodes a valid surrogate pair as its astral code point', () => {
    // U+1F600 GRINNING FACE = f0 9f 98 80
    expect(bytesOf('\ud83d\ude00')).toEqual([0xf0, 0x9f, 0x98, 0x80]);
  });
});
