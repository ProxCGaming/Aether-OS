import React from 'react';
import { CAPABILITY_FILTERS } from './capabilityFilter';

// D4-1: the ONE search/filter toolbar for capability lists — the
// Batch 1 pattern (search input, filter chips, result count,
// clear-all) shared by the Tools, Skills, Plugins and MCP panels.
// Controlled: the panel owns the query/filter state and the
// filtering itself (via capabilityFilter.js); this component is
// only the chrome, identical on every surface.
export default function CapabilityFilterBar({
  query,
  onQueryChange,
  filter,
  onFilterChange,
  counts,
  resultCount,
  totalCount,
  searchPlaceholder = 'Search by name…',
  testId = 'capability-filter-bar',
}) {
  const isFiltering = Boolean(query && query.trim()) || filter !== 'all';
  return (
    <div data-testid={testId} style={{ marginBottom: '16px' }}>
      <input
        type="text"
        className="aether-search-input"
        aria-label={searchPlaceholder}
        placeholder={searchPlaceholder}
        value={query}
        onChange={(e) => onQueryChange(e.target.value)}
        style={{ width: '100%', padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--bg-base)', border: '1px solid var(--border-default)', color: 'var(--text-primary)', fontSize: '13px', marginBottom: '10px' }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        {CAPABILITY_FILTERS.map(({ id, label }) => {
          const selected = filter === id;
          return (
            <button
              key={id}
              onClick={() => onFilterChange(id)}
              aria-pressed={selected}
              data-filter-chip={id}
              style={{
                padding: '5px 12px',
                borderRadius: '999px',
                fontSize: '12px',
                fontWeight: selected ? 600 : 400,
                cursor: 'pointer',
                background: selected ? 'rgba(var(--accent-rgb), 0.18)' : 'var(--bg-base)',
                color: selected ? 'var(--accent)' : 'var(--text-secondary)',
                border: `1px solid ${selected ? 'rgba(var(--accent-rgb), 0.45)' : 'var(--border-default)'}`,
              }}
            >
              {label} · {counts ? counts[id] : 0}
            </button>
          );
        })}
        <span style={{ fontSize: '12px', color: 'var(--text-muted)', marginLeft: '4px' }}>
          Showing {resultCount} of {totalCount}
        </span>
        {isFiltering && (
          <button
            onClick={() => { onQueryChange(''); onFilterChange('all'); }}
            style={{ background: 'transparent', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontSize: '12px', padding: '4px 6px' }}
          >
            Clear search &amp; filters
          </button>
        )}
      </div>
    </div>
  );
}
