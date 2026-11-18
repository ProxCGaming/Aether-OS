// D4-8: the ONE enable/disable mechanism for capability surfaces.
//
// Enable/disable used to be four different controls with two
// different reliability semantics (FF4-08): Skills and Tools went
// through the shell's queued send, MCP and Plugins called the
// socket directly and a click while the engine was down was lost
// with no sign it had ever happened.
//
// The mechanism is now a single contract, and this module is its
// shared vocabulary:
//
//   SEND — a toggle is a USER ACTION, so it rides safeSend (the
//   socket context's queued send; the Settings shell wraps it as
//   sendSettingsAction). safeSend builds the standard envelope,
//   sends when the engine is live and QUEUES when it is not — the
//   action is never silently dropped, and the queue flushes, in
//   order, on reconnect.
//
//   STATE — a surface never keeps a local "pending" flag. Whether
//   a toggle is queued is DERIVED from the real offline queue
//   (pendingMessages, the context's reactive view of that queue)
//   via pendingEnableChange below, so the affordance clears
//   itself the moment the queue flushes — it cannot drift from
//   the truth.
//
// Every surface's toggle is the same wire shape — a request type
// plus a payload carrying the item's name and the target
// `enabled` — so one derivation serves all four:
//
//   Skills  SKILL_TOGGLE_REQUEST        { name, enabled }
//   Plugins PLUGIN_TOGGLE_REQUEST       { name, enabled }
//   MCP     MCP_SERVER_UPDATE_REQUEST   { name, enabled }
//   Tools   TOOL_ENABLED_SET_REQUEST    { tool_name, enabled }
//
// (Tools keys its payload by `tool_name`, matching its policy
// sibling TOOL_POLICY_SET_REQUEST; the derivation takes the key
// as a parameter rather than special-casing it.)

export const ENABLE_TOGGLE_TYPES = {
  skill: 'SKILL_TOGGLE_REQUEST',
  plugin: 'PLUGIN_TOGGLE_REQUEST',
  mcpServer: 'MCP_SERVER_UPDATE_REQUEST',
  tool: 'TOOL_ENABLED_SET_REQUEST',
};

// The queued enable change for one item, or undefined when no
// queued message toggles it. `pendingMessages` entries are the
// queue descriptors ({ type, payload }) the socket context
// publishes — the same derivation the Settings shell's
// pending*For helpers perform.
export function pendingEnableChange(pendingMessages, type, nameKey, name) {
  return (pendingMessages || []).find(
    (m) => m && m.type === type && m.payload && m.payload[nameKey] === name
  );
}

// The verb a queued change will apply — 'Enable' or 'Disable' —
// read off the queued payload itself, so the note beside a
// control names the action that is actually waiting.
export function queuedEnableVerb(entry) {
  return entry && entry.payload && entry.payload.enabled ? 'Enable' : 'Disable';
}
