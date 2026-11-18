import React from 'react';
import { FIT_BADGES } from '../modelHubLabels';

// Spec D (FF3-08): the shared model-state badges for the hub,
// the downloaded inventory and the engine's own list — one
// rendering for the words every serving backend shares, so the
// surfaces cannot drift apart when the backend changes.
//
// FitBadge renders the engine-judged fit verdict carried on a
// row's `fit` field ({tier, reason} from machine_capability).
// No fit on the row — size unknown, or the machine's budget
// unmeasured — renders NOTHING: an unjudged model gets no
// badge, never a guessed tier.
//
// LoadStateBadge renders the backend's residency report: a
// row whose payload carries `loaded: true` is resident in the
// backend's memory ("Loaded"), `loaded: false` is on disk
// only. That is a DIFFERENT fact from "Active" (the configured
// default the router serves): a model can be loaded without
// being active, and active without being loaded yet. When the
// payload carries no `loaded` field at all — the backend
// cannot report residency — the badge omits rather than
// guessing from the selection.

const pillStyle = (badge) => ({
  background: badge.bg,
  color: badge.color,
  border: `1px solid ${badge.border}`,
  padding: '4px 8px',
  borderRadius: '20px',
  fontSize: '11px',
  fontWeight: 600,
  letterSpacing: '0.03em',
  whiteSpace: 'nowrap',
});

export default function FitBadge({ fit, testId }) {
  if (!fit || !FIT_BADGES[fit.tier]) return null;
  const badge = FIT_BADGES[fit.tier];
  return (
    <span data-testid={testId} title={fit.reason || ''} style={pillStyle(badge)}>
      {badge.label}
    </span>
  );
}

const LOADED_BADGE = {
  label: 'Loaded',
  color: 'var(--accent)',
  bg: 'rgba(var(--accent-rgb), 0.12)',
  border: 'rgba(var(--accent-rgb), 0.35)',
};

const ON_DISK_BADGE = {
  label: 'On disk',
  color: 'var(--text-muted)',
  bg: 'rgba(148, 148, 143, 0.12)',
  border: 'rgba(148, 148, 143, 0.3)',
};

export function LoadStateBadge({ loaded, testId }) {
  if (loaded !== true && loaded !== false) return null;
  const badge = loaded ? LOADED_BADGE : ON_DISK_BADGE;
  return (
    <span
      data-testid={testId}
      title={loaded
        ? 'Resident in the serving backend’s memory right now'
        : 'Downloaded, but not currently loaded in the serving backend'}
      style={pillStyle(badge)}
    >
      {badge.label}
    </span>
  );
}
