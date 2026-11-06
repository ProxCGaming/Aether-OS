// Shared byte-size formatting (Wave 5, deslop C2): "render a byte
// count for a human" is one rule, and it used to live in three
// drifted copies — MediaLibrary (full unit table, clamped index),
// LocalModelsTab (capped at TB, null for missing) and
// WorkspaceIndexingPanel (capped at GB, index UNCLAMPED — at
// ≥ 1 TB it indexed past its unit list and rendered
// "1024 undefined": the exact D3-24 bug, still live there).
// This module carries the MediaLibrary semantics, the fullest of
// the three; the copies are deleted and every site imports here.

export function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const k = 1024;
  // D3-24: the unit list runs to PB and the index is clamped into
  // it — the old four-unit list indexed past its end at ≥ 1 TB
  // and rendered "1024 undefined".
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), sizes.length - 1);
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

export default formatBytes;
