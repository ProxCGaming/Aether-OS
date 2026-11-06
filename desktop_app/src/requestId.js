// Shared request-id minting for engine envelopes.
//
// D3-10: a request_id is a CORRELATION id — the engine answers
// against it and parks state under it (plugin approvals are the
// case in point in SocketContext). Minting with Date.now() alone
// made every action sent inside the same millisecond share one
// id, so the ids did not distinguish the actions they exist to
// correlate. The minted shape keeps the timestamp (readable,
// roughly sortable) and adds a per-session monotonic counter —
// uniqueness within the session is guaranteed by the counter,
// never by clock resolution — plus a short random suffix so ids
// from different app runs cannot collide either.
let requestIdCounter = 0;

export function mintRequestId() {
  requestIdCounter += 1;
  const random = Math.random().toString(36).slice(2, 8);
  return `${Date.now().toString(36)}-${requestIdCounter.toString(36)}-${random}`;
}

export default mintRequestId;
