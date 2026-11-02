import React, { useEffect, useMemo, useRef, useState } from 'react';

/**
 * Command palette (Epic 6, E6-03). Opened with Ctrl+K.
 *
 * Items are supplied by the host (actions, sessions, models) as
 * { id, kind, label, hint?, run }. Filtering is a case-insensitive
 * subsequence match over "kind label hint" — forgiving of word
 * order gaps, strict enough to stay predictable. Keyboard: arrows
 * move, Enter runs, Esc closes. Only items whose run() does
 * something real are ever supplied — no placeholder entries.
 */

export function filterPaletteItems(items, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return items.slice(0, 12);
  const scored = [];
  for (const item of items) {
    const hay = `${item.kind} ${item.label} ${item.hint || ''}`.toLowerCase();
    // Subsequence match: every query char appears in order.
    let hi = 0;
    let ok = true;
    let gaps = 0;
    for (const ch of q) {
      const found = hay.indexOf(ch, hi);
      if (found === -1) {
        ok = false;
        break;
      }
      gaps += found - hi;
      hi = found + 1;
    }
    if (ok) scored.push({ item, gaps });
  }
  scored.sort((a, b) => a.gaps - b.gaps);
  return scored.slice(0, 12).map((s) => s.item);
}

export default function CommandPalette({ open, items = [], onClose }) {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(0);
  const inputRef = useRef(null);

  const results = useMemo(() => filterPaletteItems(items, query), [items, query]);

  useEffect(() => {
    if (open) {
      setQuery('');
      setSelected(0);
      // Focus after mount so typing lands in the palette.
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open ]);

  useEffect(() => {
    setSelected(0);
  }, [query]);

  if (!open) return null;

  const runItem = (item) => {
    if (!item) return;
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

  return (
    <div
      data-testid="command-palette"
      style={{
        position: 'fixed', inset: 0, background: 'rgba(5,5,10,0.6)',
        display: 'flex', justifyContent: 'center', alignItems: 'flex-start',
        paddingTop: '12vh', zIndex: 210,
      }}
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label="Command palette"
        style={{
          width: '560px', maxWidth: '92vw', background: 'rgba(20,20,28,0.98)',
          border: '1px solid rgba(255,255,255,0.12)', borderRadius: '16px',
          overflow: 'hidden', color: '#f0f0f5',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          aria-label="Search commands"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Type a command, chat, or model…"
          style={{
            width: '100%', boxSizing: 'border-box', padding: '16px',
            background: 'transparent', border: 'none', borderBottom: '1px solid rgba(255,255,255,0.08)',
            color: '#fff', fontSize: '15px', outline: 'none',
          }}
        />
        <div style={{ maxHeight: '46vh', overflowY: 'auto' }}>
          {results.length === 0 && (
            <div style={{ padding: '16px', color: '#9090a0', fontSize: '14px' }}>No matches.</div>
          )}
          {results.map((item, idx) => (
            <button
              key={item.id}
              data-testid={`palette-item-${item.id}`}
              onMouseEnter={() => setSelected(idx)}
              onClick={() => runItem(item)}
              style={{
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                width: '100%', textAlign: 'left', padding: '11px 16px', cursor: 'pointer',
                background: idx === selected ? 'rgba(124,58,237,0.22)' : 'transparent',
                border: 'none', color: '#f0f0f5', fontSize: '14px',
              }}
            >
              <span>{item.label}</span>
              <span style={{ color: '#9090a0', fontSize: '12px', textTransform: 'capitalize' }}>{item.hint || item.kind}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
