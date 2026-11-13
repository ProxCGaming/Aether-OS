/**
 * OS notification decisions for engine events (Epic 6, E6-01).
 *
 * Pure logic: the main process forwards every engine message to the
 * renderer; it also watches for three event types worth an OS
 * notification — a task finishing, an approval being needed, and a
 * calendar reminder coming due — but ONLY when the user is not
 * already looking at the app (window hidden or unfocused).
 * Everything else stays in-app.
 */

const NOTIFY_TYPES = new Set(['TASK_COMPLETED', 'TOOL_APPROVAL_REQUEST', 'REMINDER_DUE']);

function parseType(rawMessage) {
  try {
    const data = typeof rawMessage === 'string' ? JSON.parse(rawMessage) : rawMessage;
    return data && typeof data.type === 'string' ? data : null;
  } catch {
    return null;
  }
}

function shouldNotify(rawMessage, { windowVisible, windowFocused } = {}) {
  const data = parseType(rawMessage);
  if (!data || !NOTIFY_TYPES.has(data.type)) return false;
  if (windowVisible && windowFocused) return false; // already looking at it
  return true;
}

function notificationContent(rawMessage) {
  const data = parseType(rawMessage);
  if (!data) return null;
  if (data.type === 'TASK_COMPLETED') {
    return { title: 'Aether-OS — task complete', body: 'A delegated task has finished.' };
  }
  if (data.type === 'TOOL_APPROVAL_REQUEST') {
    const tool = data.payload && data.payload.tool_name ? String(data.payload.tool_name) : 'a tool';
    return { title: 'Aether-OS — approval needed', body: `${tool} is waiting for your approval.` };
  }
  if (data.type === 'REMINDER_DUE') {
    const p = data.payload || {};
    const title = p.title ? String(p.title) : 'An event';
    const when = p.start_text ? String(p.start_text) : '';
    const lead = p.kind === 'day_before' ? 'Tomorrow' : 'Coming up';
    return { title: 'Aether-OS — reminder', body: `${lead}: ${title}${when ? ` — ${when}` : ''}` };
  }
  return null;
}

module.exports = { shouldNotify, notificationContent, NOTIFY_TYPES };
