import React from 'react';

// Full Furnish S-9 (FF1-14): the ONE empty-state pattern, modelled on
// MediaLibrary's best-in-app example — a centred column with an icon
// medallion, a title, one line of explanation, and at most ONE
// action. Before this component every surface improvised its empty
// state (bare one-liners with no icon and no way forward); new
// surfaces use this instead of inventing their own.
//
// Variants:
//   'empty'     — nothing exists yet (the default; icon medallion).
//   'no-results'— a search/filter matched nothing (plain icon, no
//                 medallion — MediaLibrary's search-empty treatment).
// `compact` shrinks the pattern for narrow lists/panels (no
// medallion, smaller title) so it fits sidebars like the artifacts
// list.
//
// The CTA is optional and single by design: an empty state teaches
// the ONE next action, it is not a toolbar.
export default function EmptyState({
  icon,
  title,
  description,
  actionLabel,
  onAction,
  variant = 'empty',
  compact = false,
  testId,
}) {
  const showMedallion = variant === 'empty' && !compact;
  return (
    <div
      data-testid={testId}
      data-empty-variant={variant}
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: compact ? '6px' : '10px',
        padding: compact ? '16px 12px' : '32px 24px',
        color: 'var(--text-muted)',
        textAlign: 'center',
      }}
    >
      {icon && (showMedallion ? (
        <div
          aria-hidden="true"
          style={{
            width: '56px',
            height: '56px',
            borderRadius: '50%',
            background: 'var(--bg-raised)',
            border: '1px solid var(--border-default)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          {icon}
        </div>
      ) : (
        <div aria-hidden="true" style={{ display: 'flex' }}>{icon}</div>
      ))}
      <div style={{ fontSize: compact ? '13px' : '15px', fontWeight: 500, color: 'var(--text-primary)' }}>
        {title}
      </div>
      {description && (
        <div style={{ fontSize: '12px', color: 'var(--text-muted)', maxWidth: '360px' }}>
          {description}
        </div>
      )}
      {actionLabel && onAction && (
        <button
          onClick={onAction}
          style={{
            marginTop: '4px',
            background: 'var(--accent)',
            color: 'var(--accent-on)',
            border: 'none',
            padding: '8px 18px',
            borderRadius: '10px',
            cursor: 'pointer',
            fontWeight: 500,
            fontSize: '13px',
          }}
        >
          {actionLabel}
        </button>
      )}
    </div>
  );
}
