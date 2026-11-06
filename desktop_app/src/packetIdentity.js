// Dedup identity for raw engine packets (SocketContext delivery).
//
// A bounded digest of the packet bytes: length plus two
// independent 32-bit hashes. D3-11: the dedup key used to embed
// the ENTIRE raw packet, so every MB-sized base64 media/artifact
// packet inside the window was retained whole, per subscriber.
// The digest carries the same distinguishing power the bytes did
// — two instances sharing an id and a ts but differing in content
// still produce different keys — at a few dozen characters.
function packetDigest(data) {
  let h1 = 0x811c9dc5; // FNV-1a offset basis
  let h2 = 0x01000193;
  for (let i = 0; i < data.length; i++) {
    const c = data.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193); // FNV prime
    h2 = Math.imul(h2 ^ c, 0x27d4eb2d);
  }
  return `${data.length}:${(h1 >>> 0).toString(36)}:${(h2 >>> 0).toString(36)}`;
}

// The dedup key for one raw engine packet, or null when the packet has
// no identity to key on (unparseable, or neither id nor ts present).
export function packetIdentityKey(data) {
  let parsed;
  try {
    parsed = JSON.parse(data);
  } catch (e) {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  const id = parsed.event_id ?? parsed.request_id ?? null;
  const ts = parsed.ts ?? null;
  if (id === null && ts === null) return null;
  // The bytes ride along AS A DIGEST so two instances that share an
  // id and a ts but differ in content still count as distinct
  // packets; only a byte-identical redelivery of an already-
  // delivered identity is a duplicate.
  return `${id}|${ts}|${parsed.type ?? ''}|${packetDigest(data)}`;
}

export default packetIdentityKey;
