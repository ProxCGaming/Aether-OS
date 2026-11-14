import React from 'react';
import { Layers } from 'lucide-react';

// Epic 8, E8-A8.1b: Segmented Context Visualizer and Compact Header Bar.
//
// The 7 itemised context segments defined in PRD §E8-A8.1:
// system prompt, memory briefing, history, tool outputs, scratchpad, repo map, skill catalogue.

export const COMPONENT_COLORS = {
  'system prompt': '#a78bfa',
  'memory briefing': '#f472b6',
  'history': '#60a5fa',
  'tool outputs': '#f59e0b',
  'scratchpad': '#34d399',
  'repo map': '#38bdf8',
  'skill catalogue': '#fb7185',
};

// N14: segment shares are shares of the model's context CAPACITY
// (the whole window), never of the tokens used so far — a component
// occupying 25% of the window renders 25% however full the window is.
// The capacity comes from the breakdown payload itself
// (context_limit, measured by the engine in M20). When the payload
// carries no usable limit the window size is genuinely unknown: only
// then do shares fall back to used-relative, and callers surface that
// basis explicitly (see the basis note in ContextBar) rather than
// dividing by a fabricated denominator.
export function contextCapacity(breakdown) {
  const limit = breakdown?.context_limit;
  return typeof limit === 'number' && limit > 0 ? limit : null;
}

export function contextShareBasis(breakdown) {
  const capacity = contextCapacity(breakdown);
  if (capacity != null) return { basis: 'capacity', denominator: capacity };
  return { basis: 'used', denominator: breakdown?.total_tokens || 0 };
}

// Percentage share of one component on the basis above, or null when
// the component is unmeasured (M20: tokens null / unknown true) — an
// unknown component has no share and must not distort the others.
export function componentSharePct(component, breakdown) {
  if (!component || component.unknown === true || component.tokens == null) return null;
  const { denominator } = contextShareBasis(breakdown);
  return denominator > 0 ? (component.tokens / denominator) * 100 : 0;
}

export function CompactContextBar({ breakdown, defaultModel = '', onClick }) {
  // S21 (D2-38 sibling): the model named here is the breakdown's
  // own model, or — before any breakdown exists — the caller's
  // configured default (DraggableChatWindow derives it from the
  // provider list's is_default entry, the same source as its
  // model picker). The old hard-coded 'gemini-3.5-flash'
  // fallback named a model the user may never have configured.
  const model = breakdown?.model || defaultModel || '';
  const totalTokens = breakdown?.total_tokens || 0;
  const capacity = contextCapacity(breakdown);
  const percentUsed = breakdown?.percent_used != null ? breakdown.percent_used : 0;
  // S21: no breakdown is its own state. The old render showed
  // the fallback model with a 0% meter — an unmeasured session
  // impersonating a measured, empty one. Now the meter stays
  // empty and the percentage reads '—' until the engine reports.
  const hasBreakdown = breakdown != null;

  return (
    <button
      type="button"
      data-testid="compact-context-bar"
      onClick={onClick}
      title={!hasBreakdown
        ? (model
          ? `Default model context: ${model} — no breakdown measured yet`
          : 'Context: no breakdown measured yet')
        : capacity != null
          ? `Default model context: ${model} (${totalTokens.toLocaleString()} / ${capacity.toLocaleString()} tokens - ${percentUsed}%)`
          : `Default model context: ${model} (${totalTokens.toLocaleString()} tokens, window size unknown)`}
      style={{
        background: 'var(--bg-hover)',
        border: '1px solid var(--border-default)',
        color: 'var(--text-primary)',
        cursor: 'pointer',
        padding: '5px 10px',
        borderRadius: '999px',
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        transition: 'all 0.2s',
        fontSize: '12px',
        fontWeight: 500,
      }}
    >
      <Layers size={13} style={{ color: '#F5B942' }} />
      <span style={{ color: 'var(--text-secondary)', fontSize: '11px', maxWidth: '110px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {model || 'Context'}
      </span>
      <div
        style={{
          width: '42px',
          height: '5px',
          background: 'var(--bg-hover)',
          borderRadius: '3px',
          overflow: 'hidden',
          display: 'flex',
        }}
      >
        {hasBreakdown && (
          <div
            style={{
              width: `${Math.min(100, Math.max(percentUsed > 0 ? 3 : 0, percentUsed))}%`,
              background: percentUsed > 90 ? '#ef4444' : (percentUsed > 70 ? '#f59e0b' : '#38bdf8'),
              borderRadius: '3px',
              transition: 'width 0.3s ease',
            }}
          />
        )}
      </div>
      <span data-testid="compact-context-percent" style={{ fontSize: '11px', color: 'var(--text-primary)', fontWeight: 600 }}>
        {hasBreakdown ? `${percentUsed}%` : '—'}
      </span>
    </button>
  );
}

const panelStyle = {
  position: 'absolute',
  top: '100%',
  right: 0,
  marginTop: '6px',
  background: 'rgba(16, 16, 18, 0.66)',
  backdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)',
  WebkitBackdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)',
  border: '1px solid var(--glass-edge)',
  borderRadius: 'var(--radius-lg)',
  padding: '12px 14px',
  minWidth: '340px',
  maxWidth: '400px',
  boxShadow: 'inset 0 1px 0 var(--glass-specular), 0 16px 40px rgba(0, 0, 0, 0.45)',
  color: 'var(--text-primary)',
  fontSize: '12px',
  zIndex: 1100,
};

export default function ContextBar({ breakdown = null, defaultModel = '' }) {
  // S21: same rule as the compact bar — the model is the
  // breakdown's own, else the configured default the caller
  // passes, never a hard-coded literal.
  const model = breakdown?.model || defaultModel || '';
  const totalTokens = breakdown?.total_tokens || 0;
  const capacity = contextCapacity(breakdown);
  const { basis } = contextShareBasis(breakdown);
  const percentUsed = breakdown?.percent_used != null ? breakdown.percent_used : 0;
  const components = breakdown?.components || [];

  // S21: the real no-breakdown state. Before the first
  // CONTEXT_BREAKDOWN_RESPONSE the panel used to render the
  // full meter over zeroed/fabricated data; now it says plainly
  // that nothing has been measured yet, under the configured
  // default model's name.
  if (breakdown == null) {
    return (
      <div data-testid="context-bar-panel" style={panelStyle}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <Layers size={14} style={{ color: '#F5B942' }} />
            <span style={{ fontWeight: 600, fontSize: '13px' }}>Context Window</span>
          </div>
          {model && (
            <span style={{ color: 'var(--text-muted)', fontSize: '11px' }}>
              {model}
            </span>
          )}
        </div>
        <div data-testid="context-no-breakdown" style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
          No context breakdown yet — the engine reports how the window is being
          used once it measures a task in this session.
        </div>
      </div>
    );
  }

  return (
    <div
      data-testid="context-bar-panel"
      style={panelStyle}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <Layers size={14} style={{ color: '#F5B942' }} />
          <span style={{ fontWeight: 600, fontSize: '13px' }}>Context Window</span>
        </div>
        <span style={{ color: 'var(--text-muted)', fontSize: '11px' }}>
          {model}
        </span>
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px', fontSize: '11px', color: 'var(--text-secondary)' }}>
        <span>{capacity != null
          ? `${totalTokens.toLocaleString()} / ${capacity.toLocaleString()} tokens`
          : `${totalTokens.toLocaleString()} tokens`}</span>
        <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{capacity != null ? `${percentUsed}%` : '—'}</span>
      </div>

      {/* Segmented Bar */}
      <div
        data-testid="context-segmented-bar"
        style={{
          width: '100%',
          height: '10px',
          background: 'var(--bg-hover)',
          borderRadius: '5px',
          overflow: 'hidden',
          display: 'flex',
          marginBottom: '12px',
        }}
      >
        {totalTokens === 0 ? (
          <div style={{ width: '100%', height: '100%', background: 'var(--bg-hover)' }} />
        ) : (
          components.map((c) => {
            // Unmeasured components (M20) get no segment: their size
            // is unknown, and inventing a width would distort the
            // measured segments' capacity shares.
            if (c.unknown === true || c.tokens == null || c.tokens <= 0) return null;
            const pct = componentSharePct(c, breakdown);
            const color = COMPONENT_COLORS[c.name] || '#94a3b8';
            const slug = c.name.replace(/\s+/g, '-');
            return (
              <div
                key={c.name}
                data-testid={`context-segment-${slug}`}
                title={`${c.name}: ${c.tokens.toLocaleString()} tokens (${pct.toFixed(1)}% of ${basis === 'capacity' ? 'window' : 'used tokens'})`}
                style={{
                  width: `${pct}%`,
                  height: '100%',
                  background: color,
                  transition: 'width 0.3s ease',
                }}
              />
            );
          })
        )}
      </div>

      {basis === 'used' && (
        <div
          data-testid="context-share-basis-note"
          style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '-6px', marginBottom: '10px' }}
        >
          Window size unknown — shares are of used tokens, not of the full window.
        </div>
      )}

      {/* Itemised Breakdown List */}
      <div
        data-testid="context-breakdown-list"
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: '6px',
          borderTop: '1px solid var(--border-default)',
          paddingTop: '8px',
        }}
      >
        {components.map((c) => {
          const color = COMPONENT_COLORS[c.name] || '#94a3b8';
          const slug = c.name.replace(/\s+/g, '-');
          // M20: a component the engine could not measure arrives as
          // tokens: null / unknown: true — render it as unknown,
          // never as a fabricated 0.
          const isUnknown = c.unknown === true || c.tokens == null;
          // N14: the listed share uses the same basis as the bar —
          // capacity when the payload carries the model's limit,
          // used-relative only as the explicit fallback.
          const sharePct = isUnknown ? '—' : componentSharePct(c, breakdown).toFixed(1);
          return (
            <div
              key={c.name}
              data-testid={`context-item-${slug}`}
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                fontSize: '11px',
                color: c.tokens > 0 ? 'var(--text-primary)' : 'var(--text-muted)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span
                  style={{
                    width: '8px',
                    height: '8px',
                    borderRadius: '50%',
                    background: c.tokens > 0 ? color : 'var(--border-strong)',
                    display: 'inline-block',
                  }}
                />
                <span style={{ textTransform: 'capitalize' }}>{c.name}</span>
              </div>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <span style={{ color: 'var(--text-muted)' }}>{isUnknown ? sharePct : `${sharePct}%`}</span>
                <span style={{ fontWeight: 500, minWidth: '45px', textAlign: 'right' }}>
                  {isUnknown ? 'unknown' : c.tokens.toLocaleString()}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
