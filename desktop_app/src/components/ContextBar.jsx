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

export function CompactContextBar({ breakdown, onClick }) {
  const model = breakdown?.model || 'gemini-3.5-flash';
  const totalTokens = breakdown?.total_tokens || 0;
  const capacity = contextCapacity(breakdown);
  const percentUsed = breakdown?.percent_used != null ? breakdown.percent_used : 0;

  return (
    <button
      type="button"
      data-testid="compact-context-bar"
      onClick={onClick}
      title={capacity != null
        ? `Default model context: ${model} (${totalTokens.toLocaleString()} / ${capacity.toLocaleString()} tokens - ${percentUsed}%)`
        : `Default model context: ${model} (${totalTokens.toLocaleString()} tokens, window size unknown)`}
      style={{
        background: 'rgba(255, 255, 255, 0.08)',
        border: '1px solid rgba(255, 255, 255, 0.12)',
        color: '#fff',
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
      <Layers size={13} style={{ color: '#a78bfa' }} />
      <span style={{ color: 'rgba(255, 255, 255, 0.75)', fontSize: '11px', maxWidth: '110px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {model}
      </span>
      <div
        style={{
          width: '42px',
          height: '5px',
          background: 'rgba(255, 255, 255, 0.15)',
          borderRadius: '3px',
          overflow: 'hidden',
          display: 'flex',
        }}
      >
        <div
          style={{
            width: `${Math.min(100, Math.max(percentUsed > 0 ? 3 : 0, percentUsed))}%`,
            background: percentUsed > 90 ? '#ef4444' : (percentUsed > 70 ? '#f59e0b' : '#38bdf8'),
            borderRadius: '3px',
            transition: 'width 0.3s ease',
          }}
        />
      </div>
      <span style={{ fontSize: '11px', color: '#fff', fontWeight: 600 }}>
        {percentUsed}%
      </span>
    </button>
  );
}

export default function ContextBar({ breakdown = null }) {
  const model = breakdown?.model || 'gemini-3.5-flash';
  const totalTokens = breakdown?.total_tokens || 0;
  const capacity = contextCapacity(breakdown);
  const { basis } = contextShareBasis(breakdown);
  const percentUsed = breakdown?.percent_used != null ? breakdown.percent_used : 0;
  const components = breakdown?.components || [];

  return (
    <div
      data-testid="context-bar-panel"
      style={{
        position: 'absolute',
        top: '100%',
        right: 0,
        marginTop: '6px',
        background: 'rgba(23, 23, 30, 0.95)',
        backdropFilter: 'blur(20px)',
        border: '1px solid rgba(255, 255, 255, 0.12)',
        borderRadius: '8px',
        padding: '12px 14px',
        minWidth: '340px',
        maxWidth: '400px',
        boxShadow: '0 8px 32px rgba(0, 0, 0, 0.45)',
        color: '#fff',
        fontSize: '12px',
        zIndex: 1100,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <Layers size={14} style={{ color: '#a78bfa' }} />
          <span style={{ fontWeight: 600, fontSize: '13px' }}>Context Window</span>
        </div>
        <span style={{ color: 'rgba(255, 255, 255, 0.65)', fontSize: '11px' }}>
          {model}
        </span>
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px', fontSize: '11px', color: 'rgba(255, 255, 255, 0.75)' }}>
        <span>{capacity != null
          ? `${totalTokens.toLocaleString()} / ${capacity.toLocaleString()} tokens`
          : `${totalTokens.toLocaleString()} tokens`}</span>
        <span style={{ fontWeight: 600, color: '#fff' }}>{capacity != null ? `${percentUsed}%` : '—'}</span>
      </div>

      {/* Segmented Bar */}
      <div
        data-testid="context-segmented-bar"
        style={{
          width: '100%',
          height: '10px',
          background: 'rgba(255, 255, 255, 0.1)',
          borderRadius: '5px',
          overflow: 'hidden',
          display: 'flex',
          marginBottom: '12px',
        }}
      >
        {totalTokens === 0 ? (
          <div style={{ width: '100%', height: '100%', background: 'rgba(255, 255, 255, 0.05)' }} />
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
          style={{ fontSize: '11px', color: 'rgba(255, 255, 255, 0.5)', marginTop: '-6px', marginBottom: '10px' }}
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
          borderTop: '1px solid rgba(255, 255, 255, 0.08)',
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
                color: c.tokens > 0 ? '#fff' : 'rgba(255, 255, 255, 0.4)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span
                  style={{
                    width: '8px',
                    height: '8px',
                    borderRadius: '50%',
                    background: c.tokens > 0 ? color : 'rgba(255, 255, 255, 0.2)',
                    display: 'inline-block',
                  }}
                />
                <span style={{ textTransform: 'capitalize' }}>{c.name}</span>
              </div>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <span style={{ color: 'rgba(255, 255, 255, 0.5)' }}>{isUnknown ? sharePct : `${sharePct}%`}</span>
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
