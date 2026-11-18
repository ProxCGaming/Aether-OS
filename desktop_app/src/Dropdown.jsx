import React, { useState, useEffect, useRef } from 'react';
import { ChevronDown, Check, SearchX } from 'lucide-react';
import EmptyState from './components/EmptyState';

// Full Furnish Spec C (FF3-07): the dropdown is the shared picker
// core. Two opt-in upgrades, both off by default so existing
// consumers render exactly as before:
//
// - `searchable` adds the Wave 3 search pattern (the command
//   palette's idiom): a filter input at the head of the menu that
//   narrows items by label / meta / badge / group name, and the
//   shared EmptyState no-results treatment with a one-click
//   "Clear search" recovery when nothing matches.
// - Item objects may carry `meta` (a second, muted line under the
//   label) and `badge` ({ text, tone }) rendered as a small pill —
//   the model pickers use them for capability/cost lines and the
//   learned-unusable badge. Absent fields render absent.
export default function Dropdown({
  value,
  onChange,
  options = [],
  placeholder = 'Select...',
  style = {},
  align = 'left',
  testId,
  searchable = false,
  searchPlaceholder = 'Search…',
  noResultsTitle = 'No matches'
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const dropdownRef = useRef(null);
  const scrollRef = useRef(null);
  const searchRef = useRef(null);

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    };
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen]);

  // A closed menu forgets its filter: reopening always starts
  // from the full list, like the palette's clear-on-close.
  useEffect(() => {
    if (!isOpen) setQuery('');
  }, [isOpen]);

  // The search input takes focus when a searchable menu opens —
  // the whole point is type-to-filter.
  useEffect(() => {
    if (isOpen && searchable && searchRef.current) {
      searchRef.current.focus();
    }
  }, [isOpen, searchable]);

  // Custom butter-smooth scrolling using lerp (linear interpolation)
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    let targetScroll = el.scrollTop;
    let isScrolling = false;
    let frameId;

    const updateScroll = () => {
      if (!el) return;

      // Interpolate towards the target
      el.scrollTop += (targetScroll - el.scrollTop) * 0.15;

      if (Math.abs(targetScroll - el.scrollTop) > 0.5) {
        frameId = requestAnimationFrame(updateScroll);
      } else {
        el.scrollTop = targetScroll;
        isScrolling = false;
      }
    };

    const onWheel = (e) => {
      e.preventDefault();
      // Accumulate scroll target
      targetScroll = Math.max(0, Math.min(el.scrollHeight - el.clientHeight, targetScroll + e.deltaY));

      if (!isScrolling) {
        isScrolling = true;
        frameId = requestAnimationFrame(updateScroll);
      }
    };

    const onScroll = () => {
      if (!isScrolling) {
        targetScroll = el.scrollTop;
      }
    };

    el.addEventListener('wheel', onWheel, { passive: false });
    el.addEventListener('scroll', onScroll, { passive: true });

    return () => {
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('scroll', onScroll);
      if (frameId) cancelAnimationFrame(frameId);
    };
  }, [isOpen]);

  // Find the selected label
  let selectedLabel = placeholder;
  for (const opt of options) {
    if (opt.group) {
      const found = opt.items.find(i => i.value === value);
      if (found) {
        selectedLabel = found.label;
        break;
      }
    } else {
      if (opt.value === value) {
        selectedLabel = opt.label;
        break;
      }
    }
  }

  // Search filtering: an item matches on its label, its meta line,
  // its badge text, or its group's name (so a provider name keeps
  // that provider's whole group). Groups left empty drop out.
  const q = searchable ? query.trim().toLowerCase() : '';
  const itemMatches = (item, groupName) => {
    if (!q) return true;
    const haystack = [item.label, item.meta || '', item.badge ? item.badge.text : '', groupName || '']
      .join(' ')
      .toLowerCase();
    return haystack.includes(q);
  };
  const visibleOptions = !q
    ? options
    : options.flatMap(opt => {
        if (opt.group) {
          const items = opt.items.filter(i => itemMatches(i, opt.group));
          return items.length ? [{ ...opt, items }] : [];
        }
        return itemMatches(opt, '') ? [opt] : [];
      });
  const hasVisibleItems = visibleOptions.some(opt => (opt.group ? opt.items.length > 0 : true));

  const firstEnabledMatch = () => {
    for (const opt of visibleOptions) {
      if (opt.group) {
        const hit = opt.items.find(i => !i.disabled);
        if (hit) return hit;
      } else if (!opt.disabled) {
        return opt;
      }
    }
    return null;
  };

  const handleSearchKeyDown = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      setIsOpen(false);
    } else if (e.key === 'Enter') {
      // Type-to-filter, Enter-to-pick: the first visible enabled
      // item is the pick, mirroring the palette's Enter.
      const hit = firstEnabledMatch();
      if (hit) {
        e.preventDefault();
        onChange(hit.value);
        setIsOpen(false);
      }
    }
  };

  const badgeStyle = (tone) => tone === 'danger'
    ? {
        color: 'var(--danger)',
        border: '1px solid rgba(201, 42, 42, 0.45)',
        background: 'rgba(201, 42, 42, 0.12)',
      }
    : {
        color: 'var(--text-secondary)',
        border: '1px solid var(--border-default)',
        background: 'transparent',
      };

  // One item renderer for the grouped and flat paths — the two
  // must never disagree on the component's contract (D2-19).
  const renderItem = (item) => {
    const isSelected = item.value === value;
    return (
      <button
        key={item.value}
        type="button"
        onClick={() => {
          // D2-19: grouped items honour `disabled` exactly like
          // the flat path — the two paths must not disagree on
          // the component's contract.
          if (item.disabled) return;
          onChange(item.value);
          setIsOpen(false);
        }}
        style={{
          width: '100%',
          textAlign: 'left',
          padding: '8px 10px',
          background: isSelected ? 'rgba(var(--accent-rgb), 0.2)' : 'transparent',
          border: 'none',
          borderRadius: '6px',
          color: isSelected ? 'var(--accent)' : (item.disabled ? 'var(--text-muted)' : 'var(--text-primary)'),
          fontSize: '13px',
          cursor: item.disabled ? 'not-allowed' : 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '8px',
          transition: 'background 0.2s',
          opacity: item.disabled ? 0.6 : 1
        }}
        onMouseEnter={e => {
          if (!isSelected && !item.disabled) e.currentTarget.style.background = 'var(--bg-hover)';
        }}
        onMouseLeave={e => {
          if (!isSelected) e.currentTarget.style.background = 'transparent';
        }}
        disabled={item.disabled}
        title={item.meta ? `${item.label} — ${item.meta}` : item.label}
      >
        <span style={{ display: 'flex', flexDirection: 'column', minWidth: 0, flex: 1 }}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.label}</span>
          {item.meta && (
            <span style={{ fontSize: '11px', color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: '2px' }}>
              {item.meta}
            </span>
          )}
        </span>
        {item.badge && (
          <span
            data-badge={item.badge.tone || 'muted'}
            style={{
              ...badgeStyle(item.badge.tone),
              fontSize: '10px',
              fontWeight: 600,
              letterSpacing: '0.03em',
              padding: '1px 6px',
              borderRadius: '999px',
              flexShrink: 0,
              whiteSpace: 'nowrap'
            }}
          >
            {item.badge.text}
          </span>
        )}
        {isSelected && <Check size={14} color="var(--accent)" style={{ flexShrink: 0 }} />}
      </button>
    );
  };

  return (
    <div className="nodrag" data-testid={testId} style={{ position: 'relative', width: style.width || 'auto', WebkitAppRegion: 'no-drag', cursor: 'auto' }} ref={dropdownRef}>
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '6px',
          padding: '8px 12px',
          background: 'var(--bg-base)',
          border: '1px solid var(--border-default)',
          borderRadius: 'var(--radius-md)',
          color: 'var(--text-primary)',
          fontSize: '13px',
          cursor: 'pointer',
          transition: 'all 0.2s',
          boxShadow: '0 2px 4px rgba(0,0,0,0.1)',
          width: '100%',
          ...style
        }}
        onMouseEnter={e => e.currentTarget.style.background = 'var(--bg-hover)'}
        onMouseLeave={e => e.currentTarget.style.background = 'var(--bg-base)'}
      >
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {selectedLabel}
        </span>
        <ChevronDown size={14} style={{ opacity: 0.7, transform: isOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s cubic-bezier(0.16, 1, 0.3, 1)' }} />
      </button>

      {isOpen && (
        <div
          className="ios-glass"
          style={{
            position: 'absolute',
            top: 'calc(100% + 8px)',
            [align === 'right' ? 'right' : 'left']: 0,
            minWidth: '240px',
            borderRadius: 'var(--radius-lg)',
            padding: '8px',
            boxShadow: 'inset 0 1px 0 var(--glass-specular), 0 16px 40px rgba(0, 0, 0, 0.45)',
            zIndex: 1000,
            animation: 'fadeIn 0.2s cubic-bezier(0.16, 1, 0.3, 1)',
            transformOrigin: 'top ' + align,
            overflow: 'hidden'
          }}
        >
          {searchable && (
            <input
              ref={searchRef}
              className="aether-search-input"
              data-testid={testId ? `${testId}-search` : undefined}
              aria-label={searchPlaceholder}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleSearchKeyDown}
              placeholder={searchPlaceholder}
              style={{
                width: '100%',
                boxSizing: 'border-box',
                padding: '8px 10px',
                marginBottom: '8px',
                background: 'transparent',
                border: 'none',
                borderBottom: '1px solid var(--border-default)',
                color: 'var(--text-primary)',
                fontSize: '13px',
                outline: 'none'
              }}
            />
          )}
          <div
            ref={scrollRef}
            className="chat-scroll"
            style={{
              maxHeight: '300px',
              overflowY: 'auto',
              paddingRight: '4px',
              display: 'flex',
              flexDirection: 'column'
            }}
          >
          {q && !hasVisibleItems ? (
            <EmptyState
              variant="no-results"
              compact
              icon={<SearchX size={22} color="var(--text-muted)" />}
              title={noResultsTitle}
              description={`Nothing matches “${query.trim()}”.`}
              actionLabel="Clear search"
              onAction={() => {
                setQuery('');
                if (searchRef.current) searchRef.current.focus();
              }}
              testId={testId ? `${testId}-no-results` : undefined}
            />
          ) : (
            visibleOptions.map((opt, idx) => {
              if (opt.group) {
                return (
                  <div key={opt.group || idx} style={{ marginBottom: '8px' }}>
                    <div style={{
                      position: 'sticky',
                      top: 0,
                      zIndex: 10,
                      background: 'rgba(22, 22, 24, 0.85)',
                      backdropFilter: 'blur(16px)',
                      WebkitBackdropFilter: 'blur(16px)',
                      fontSize: '10px',
                      textTransform: 'uppercase',
                      color: 'var(--text-secondary)',
                      padding: '6px 8px 4px 8px',
                      letterSpacing: '0.05em',
                      fontWeight: 600,
                      borderBottom: '1px solid var(--border-default)',
                      borderRadius: '4px 4px 0 0'
                    }}>
                      {opt.group}
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                      {opt.items.map(item => renderItem(item))}
                    </div>
                  </div>
                );
              }
              return renderItem(opt);
            })
          )}
          </div>
        </div>
      )}
    </div>
  );
}
