// Wave 5 (Annex C row 6): the approval decision logic shared by
// the standalone approval card (DraggableChatWindow), the compact
// ToolActivityCard and the browser confirmation control
// (ChatCards) — the argument-editor parser and the envelope-grant
// contract. Pure functions; the E8-1.3 / E8-6.2 comments above
// each one are their specs and travel with them.

// Epic 8, E8-1.3: parse the approval drawer's argument editor.
// Returns { editedArgs } only when the parsed JSON is a plain object
// that actually differs from the proposed args; { editedArgs: null }
// for an unchanged editor (legacy approve), and { error } for text
// that must block the approve (invalid JSON / not an object).
export function parseApprovalEditedArgs(text, originalArgs) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { editedArgs: null, error: 'Arguments must be valid JSON.' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { editedArgs: null, error: 'Arguments must be a JSON object.' };
  }
  const sortDeep = (v) => (Array.isArray(v)
    ? v.map(sortDeep)
    : (v && typeof v === 'object'
      ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortDeep(v[k])]))
      : v));
  if (JSON.stringify(sortDeep(parsed)) === JSON.stringify(sortDeep(originalArgs || {}))) {
    return { editedArgs: null, error: null };
  }
  return { editedArgs: parsed, error: null };
}

// Epic 8, E8-6.2 / N11: the ONE browser-confirmation control and
// grant contract, shared by the standalone approval card and the
// compact ToolActivityCard. Before N11 the envelope checkbox
// existed only on the standalone surface, so the same approval
// offered different powers depending on which card rendered it.
// Payments are never envelope-eligible; the engine re-scopes any
// grant to the confirmed action's category + site regardless.
export function browserEnvelopeGrant(confirmation, grantChecked) {
  if (!confirmation || confirmation.category === 'payments') return undefined;
  return grantChecked ? { grant: true } : undefined;
}

// Master build S20: the desktop-confirmation grant contract, the
// browser helper's analogue — shared by both approval surfaces
// for the same N11 reason. A desktop approval can carry TWO
// opt-ins: a per-task envelope ({ envelope: { grant: true } })
// and a remembered grant ({ remember: true }). The hard-confirm
// categories (close_or_quit, system) are eligible for NEITHER —
// the control never renders the boxes for them, and this helper
// is the second lock: a hard-confirm confirmation yields
// undefined no matter what the choice state says. Pixel-consent
// is not part of the choice: approving a pixel action IS the
// consent, recorded by the engine.
export function desktopGrantSpec(confirmation, choice) {
  if (!confirmation || confirmation.hard_confirm) return undefined;
  if (confirmation.category === 'observe' || confirmation.category === 'unknown') return undefined;
  const spec = {};
  if (choice && choice.envelope) spec.envelope = { grant: true };
  if (choice && choice.remember) spec.remember = true;
  return Object.keys(spec).length > 0 ? spec : undefined;
}
