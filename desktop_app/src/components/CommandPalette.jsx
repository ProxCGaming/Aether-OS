import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, Cpu, MessageSquare, SearchX, Settings, Zap } from 'lucide-react';
import EmptyState from './EmptyState';
import { useFocusTrap } from '../hooks/useFocusTrap';

/**
 * Command palette (Epic 6, E6-03; navigation rebuild in Full
 * Furnish S-3). Opened with Ctrl+K or the search-field trigger in
 * the sidebar head.
 *
 * Items are supplied by the host (actions, sessions, models) as
 * { id, kind, label, hint?, shortcut?, group?, run }. Only items
 * whose run() does something real are ever supplied — no
 * placeholder entries, and the run() contract is unchanged.
 *
 * The palette is a navigation surface, not a flat list: results
 * render in groups (Actions / Go to / Settings / Sessions /
 * Models) with a per-group cap, an empty query leads with the
 * items most recently run from the palette, and every row carries
 * an icon plus a right-side hint. Matching is a subsequence match
 * over "kind label hint" (as before), but the ranking now rewards
 * word-start and contiguous matches, exact label substrings, and
 * recent use — a bare gap count ranked "Settings: Voice" and a
 * stray session alike.
 *
 * Accessibility follows the ARIA combobox pattern: the input is a
 * combobox announcing the active option via aria-activedescendant,
 * the results are a listbox of options, and the shared focus trap
 * keeps Tab inside the dialog and restores focus to the invoker
 * on close. Arrow/Enter handling stays on the input, per the
 * combobox pattern.
 */

export const PALETTE_GROUP_ORDER = ['Recent', 'Actions', 'Go to', 'Settings', 'Sessions', 'Models'];
export const PALETTE_GROUP_CAP = 6;
export const PALETTE_RECENT_CAP = 5;

// Row icons key off the item's own group — a recent session still
// shows the Sessions icon inside the Recent group.
const GROUP_ICONS = {
  Actions: Zap,
  'Go to': ArrowRight,
  Settings: Settings,
  Sessions: MessageSquare,
  Models: Cpu,
};

// Groups are derived from the item kind the host already supplies
// (an explicit item.group wins when present). 'Go to …' actions
// are navigation, not actions — the host labels them as such.
export function paletteGroupFor(item) {
  if (item.group) return item.group;
  switch (item.kind) {
    case 'settings': return 'Settings';
    case 'session': return 'Sessions';
    case 'model': return 'Models';
    case 'action': return /^go to\b/i.test(item.label || '') ? 'Go to' : 'Actions';
    default: return 'Actions';
  }
}

// Positions of a subsequence match of q in hay, or null.
function subsequencePositions(hay, q) {
  const positions = [];
  let hi = 0;
  for (const ch of q) {
    const found = hay.indexOf(ch, hi);
    if (found === -1) return null;
    positions.push(found);
    hi = found + 1;
  }
  return positions;
}

// Deterministic match score for one item, or null for no match.
// Higher is better. Rewards: each matched char landing on a word
// start, contiguous runs of matched chars, and the whole query
// appearing verbatim in the label. Penalises: spread (gaps inside
// the match) and a late first match. Items the user recently ran
// from the palette get a small boost, most recent first.
export function scorePaletteMatch(item, query, recentIds = []) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return 0;
  const hay = `${item.kind} ${item.label} ${item.hint || ''}`.toLowerCase();
  const positions = subsequencePositions(hay, q);
  if (!positions) return null;
  let wordStarts = 0;
  let contiguous = 0;
  positions.forEach((p, i) => {
    if (p === 0 || !/[a-z0-9]/.test(hay[p - 1])) wordStarts += 1;
    if (i > 0 && p === positions[i - 1] + 1) contiguous += 1;
  });
  const spread = positions[positions.length - 1] - positions[0] + 1 - q.length;
  let score = wordStarts * 40 + contiguous * 8 - spread * 2 - positions[0];
  if ((item.label || '').toLowerCase().includes(q)) score += 150;
  const recentRank = recentIds.indexOf(item.id);
  if (recentRank !== -1) score += (recentIds.length - recentRank) * 10;
  return score;
}

// Flat ranked filter (the palette groups + caps its output).
// Empty query: host order, everything. Non-empty: every match,
// best score first; ties keep host order.
export function filterPaletteItems(items, query, recentIds = []) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return items.slice();
  const scored = [];
  items.forEach((item, index) => {
    const score = scorePaletteMatch(item, q, recentIds);
    if (score !== null) scored.push({ item, score, index });
  });
  scored.sort((a, b) => (b.score - a.score) || (a.index - b.index));
  return scored.map((s) => s.item);
}

// Group a flat item list for display: fixed group order, host /
// ranked order preserved inside a group, per-group cap applied.
export function groupPaletteItems(items, cap = PALETTE_GROUP_CAP) {
  const byGroup = new Map();
  for (const item of items) {
    const name = paletteGroupFor(item);
    if (!byGroup.has(name)) byGroup.set(name, []);
    byGroup.get(name).push(item);
  }
  const groups = [];
  for (const name of PALETTE_GROUP_ORDER) {
    const groupItems = byGroup.get(name);
    if (groupItems && groupItems.length > 0) {
      groups.push({ name, items: groupItems.slice(0, cap) });
    }
  }
  return groups;
}

export default function CommandPalette({ open, items = [], onClose, isConnected = true }) {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(0);
  // Ids of items run from this palette, most recent first. Kept
  // in component state (the palette stays mounted for the app's
  // lifetime), so recents are this-session only.
  const [recentIds, setRecentIds] = useState([]);
  const inputRef = useRef(null);
  const dialogRef = useRef(null);

  useFocusTrap(dialogRef, open, inputRef);

  const searching = query.trim().length > 0;

  const groups = useMemo(() => {
    if (searching) return groupPaletteItems(filterPaletteItems(items, query, recentIds));
    // Empty query: recents lead (when there are any), then the
    // grouped inventory in host order, minus the items already
    // shown as recents.
    const byId = new Map(items.map((item) => [item.id, item]));
    const recents = recentIds.map((id) => byId.get(id)).filter(Boolean).slice(0, PALETTE_RECENT_CAP);
    const rest = items.filter((item) => !recents.includes(item));
    const grouped = groupPaletteItems(rest);
    return recents.length > 0
      ? [{ name: 'Recent', items: recents }, ...grouped]
      : grouped;
  }, [items, query, recentIds, searching]);

  const results = useMemo(() => groups.flatMap((g) => g.items), [groups]);

  // Flat position of each visible item (host ids are unique), for
  // selection state shared across the group boundaries.
  const indexById = useMemo(() => {
    const map = new Map();
    results.forEach((item, i) => map.set(item.id, i));
    return map;
  }, [results]);

  useEffect(() => {
    if (open) {
      setQuery('');
      setSelected(0);
    }
  }, [open ]);

  useEffect(() => {
    setSelected(0);
  }, [query]);

  // Keep the active option in view while arrowing through a
  // scrolled list. Guarded: jsdom has no scrollIntoView.
  useEffect(() => {
    const active = results[selected];
    if (!active) return;
    const el = document.getElementById(`palette-option-${active.id}`);
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ block: 'nearest' });
    }
  }, [selected, results]);

  // D3-23: the host can swap `items` while the palette is open;
  // when the results shrink, the selection must clamp into range —
  // an out-of-range index left the palette with no visible
  // selection and an Enter that addressed nothing. Adjusted during
  // render (React's pattern for state derived from a changed
  // input) so the clamped value is what this very render uses.
  const [prevResults, setPrevResults] = useState(results);
  if (results !== prevResults) {
    setPrevResults(results);
    setSelected((s) => Math.min(s, Math.max(results.length - 1, 0)));
  }

  if (!open) return null;

  const runItem = (item) => {
    if (!item) return;
    setRecentIds((prev) => [item.id, ...prev.filter((id) => id !== item.id)].slice(0, PALETTE_RECENT_CAP));
    onClose();
    item.run();
  };

  const onKeyDown = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelected((s) => Math.min(s + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelected((s) => Math.max(s - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      runItem(results[selected]);
    }
  };

  const clearSearch = () => {
    setQuery('');
    inputRef.current?.focus();
  };

  const activeItem = results[selected];

  return (
    <div
      data-testid="command-palette"
      style={{
        position: 'fixed', inset: 0, background: 'rgba(5, 5, 6, 0.55)', backdropFilter: 'blur(6px)', WebkitBackdropFilter: 'blur(6px)',
        display: 'flex', justifyContent: 'center', alignItems: 'flex-start',
        paddingTop: '12vh', zIndex: 210,
      }}
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        style={{
          width: '560px', maxWidth: '92vw', background: 'rgba(16, 16, 18, 0.66)',
          backdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)', WebkitBackdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)',
          border: '1px solid var(--glass-edge)', borderRadius: 'var(--radius-lg)',
          boxShadow: 'inset 0 1px 0 var(--glass-specular), 0 16px 40px rgba(0, 0, 0, 0.45)',
          overflow: 'hidden', color: 'var(--text-primary)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          className="aether-search-input"
          role="combobox"
          aria-label="Search commands"
          aria-expanded={results.length > 0}
          aria-controls={results.length > 0 ? 'palette-listbox' : undefined}
          aria-activedescendant={activeItem ? `palette-option-${activeItem.id}` : undefined}
          aria-autocomplete="list"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Type a command, chat, or model…"
          style={{
            width: '100%', boxSizing: 'border-box', padding: '16px',
            background: 'transparent', border: 'none', borderBottom: '1px solid var(--border-default)',
            color: 'var(--text-primary)', fontSize: '15px',
          }}
        />
        {/* Lane C grouped S4: items are host-supplied, so while the
            engine is disconnected the palette silently offers fewer,
            staler items (actions only, stale sessions/models) with
            no sign of why. The note states the fact; item behaviour
            is unchanged — actions still run and queue via safeSend. */}
        {isConnected === false && (
          <div
            data-testid="palette-offline-note"
            role="status"
            style={{
              padding: '8px 16px', color: 'var(--accent)', fontSize: '12.5px',
              background: 'rgba(var(--accent-rgb), 0.08)',
              borderBottom: '1px solid var(--border-default)',
            }}
          >
            Engine disconnected — sessions and models may be stale, and actions will queue until it reconnects.
          </div>
        )}
        <div style={{ maxHeight: '46vh', overflowY: 'auto' }}>
          {results.length === 0 ? (
            <EmptyState
              variant="no-results"
              compact
              icon={<SearchX size={26} color="var(--text-muted)" />}
              title={searching ? 'No matches' : 'No commands available'}
              description={searching ? `Nothing matches “${query.trim()}”.` : 'There is nothing to run right now.'}
              actionLabel={searching ? 'Clear search' : undefined}
              onAction={searching ? clearSearch : undefined}
              testId="palette-no-results"
            />
          ) : (
            <div role="listbox" id="palette-listbox" aria-label="Results">
              {groups.map((group) => (
                <div key={group.name} role="group" aria-label={group.name}>
                  <div
                    aria-hidden="true"
                    style={{
                      padding: '10px 16px 4px', fontSize: '11px', fontWeight: 600,
                      textTransform: 'uppercase', letterSpacing: '1px', color: 'var(--text-muted)',
                    }}
                  >
                    {group.name}
                  </div>
                  {group.items.map((item) => {
                    const idx = indexById.get(item.id);
                    const Icon = GROUP_ICONS[paletteGroupFor(item)] || Zap;
                    return (
                      <button
                        key={item.id}
                        id={`palette-option-${item.id}`}
                        role="option"
                        aria-selected={idx === selected}
                        data-testid={`palette-item-${item.id}`}
                        onMouseEnter={() => setSelected(idx)}
                        onClick={() => runItem(item)}
                        style={{
                          display: 'flex', alignItems: 'center', gap: '10px',
                          width: '100%', textAlign: 'left', padding: '11px 16px', cursor: 'pointer',
                          background: idx === selected ? 'rgba(var(--accent-rgb), 0.18)' : 'transparent',
                          border: 'none', color: 'var(--text-primary)', fontSize: '14px',
                        }}
                      >
                        <Icon size={16} color="var(--text-muted)" style={{ flexShrink: 0 }} />
                        <span style={{ flex: 1 }}>{item.label}</span>
                        <span style={{ color: 'var(--text-muted)', fontSize: '12px', textTransform: 'capitalize' }}>{item.shortcut || item.hint || item.kind}</span>
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>
          )}
        </div>
        <div
          style={{
            display: 'flex', gap: '16px', padding: '8px 16px',
            borderTop: '1px solid var(--border-default)',
            color: 'var(--text-muted)', fontSize: '12px',
          }}
        >
          <span>↑↓ Navigate</span>
          <span>Enter Run</span>
          <span>Esc Close</span>
        </div>
      </div>
    </div>
  );
}
