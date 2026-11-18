// D4-1: the shared capability-list filter model — one definition
// of the search + filter-chip behaviour every capability panel
// (Tools, Skills, Plugins, MCP) applies, so the chips mean the
// same thing on every surface (the Batch 1 search/filter pattern,
// MERGE-A).
//
// A panel maps its own items to entries of this shape and keeps
// rendering the rows itself:
//
//   { item,        // the panel's own record, untouched
//     name,        // display name — search narrows on this
//     state,       // 'enabled' | 'disabled' | 'error'
//     hasUpdate }  // true only when update data actually exists
//
// 'error' is a state of its own (a skill whose manifest failed to
// parse, an MCP server in a failed state): it is never folded into
// enabled/disabled, and an errored item is never dropped from the
// 'all' list — the honesty rule the load-error rows already follow.
// `hasUpdate` stays false until the update checks land (Wave 5);
// until then the Updates chip honestly matches nothing and the
// panels render their no-results state for it — never fake rows.

export const CAPABILITY_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'enabled', label: 'Enabled' },
  { id: 'disabled', label: 'Disabled' },
  { id: 'error', label: 'Error' },
  { id: 'updates', label: 'Updates' },
];

export function capabilityFilterCounts(entries) {
  const counts = { all: entries.length, enabled: 0, disabled: 0, error: 0, updates: 0 };
  for (const entry of entries) {
    if (entry.state === 'enabled') counts.enabled += 1;
    else if (entry.state === 'disabled') counts.disabled += 1;
    else if (entry.state === 'error') counts.error += 1;
    if (entry.hasUpdate) counts.updates += 1;
  }
  return counts;
}

export function filterCapabilityItems(entries, query, filter) {
  const q = (query || '').trim().toLowerCase();
  return entries.filter((entry) => {
    if (q && !entry.name.toLowerCase().includes(q)) return false;
    switch (filter) {
      case 'enabled': return entry.state === 'enabled';
      case 'disabled': return entry.state === 'disabled';
      case 'error': return entry.state === 'error';
      case 'updates': return Boolean(entry.hasUpdate);
      case 'all':
      default: return true;
    }
  });
}
