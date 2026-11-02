import DOMPurify from 'dompurify';

// S3-M1: parser-based sanitisation for agent-supplied SVG.
//
// The engine only regex-strips a few patterns from scratchpad SVG
// (scratchpad.py — kept as defence in depth), and regex sanitisation
// of SVG is bypassable. Anything rendered through
// dangerouslySetInnerHTML in this privileged renderer must first go
// through DOMPurify, a real HTML/SVG parser sanitiser.
//
// Profile: SVG + SVG filters only — no HTML profile — so legitimate
// agent sketches (shapes, fills, gradients, filters, text) render,
// while script, event-handler attributes, javascript: URLs and
// foreignObject embeds (which the engine already strips by policy)
// do not survive. DOMPurify also drops SMIL elements/attributes that
// would install handlers (e.g. <set attributeName="on…">).
const SVG_SANITIZE_CONFIG = {
  USE_PROFILES: { svg: true, svgFilters: true },
  FORBID_TAGS: ['foreignObject'],
};

export function sanitizeSvg(content) {
  if (typeof content !== 'string') return '';
  return DOMPurify.sanitize(content, SVG_SANITIZE_CONFIG);
}

export default sanitizeSvg;
