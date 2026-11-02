import React from 'react';
import { render } from '@testing-library/react';
import '@testing-library/jest-dom';
import { ScratchpadPanel } from '../DraggableChatWindow';
import { sanitizeSvg } from '../sanitizeSvg';

// react-markdown is ESM-only; stub it for Jest (same pattern as the
// existing scratchpad suite).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

jest.mock('mermaid', () => ({
  __esModule: true,
  default: {
    initialize: jest.fn(),
    render: jest.fn(),
  },
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function renderSvg(content) {
  return render(
    <ScratchpadPanel scratchpad={{ kind: 'svg', content, agent: 'coder' }} />
  );
}

function injectedHtml() {
  const el = document.querySelector('.scratchpad-svg');
  expect(el).toBeTruthy();
  return el.innerHTML;
}

// S3-M1: agent-supplied SVG was only regex-sanitised on the engine and
// then injected raw via dangerouslySetInnerHTML in the privileged
// renderer. The renderer must run it through DOMPurify (SVG profile)
// immediately before injection.
describe('ScratchpadPanel SVG sanitisation (S3-M1)', () => {
  it('strips a <script> block embedded in the SVG', () => {
    renderSvg(
      '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="10" height="10"/></svg>'
    );
    const html = injectedHtml().toLowerCase();
    expect(html).not.toContain('<script');
    expect(html).not.toContain('alert(1)');
    expect(document.querySelector('.scratchpad-svg script')).toBeNull();
  });

  it('strips onload / onerror event-handler attributes', () => {
    renderSvg(
      '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><circle r="4" onerror="alert(2)"/></svg>'
    );
    const html = injectedHtml().toLowerCase();
    expect(html).not.toContain('onload');
    expect(html).not.toContain('onerror');
    expect(html).not.toContain('alert');
  });

  it('strips javascript: hrefs (href and xlink:href)', () => {
    renderSvg(
      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
      + '<a href="javascript:alert(1)"><text>click</text></a>'
      + '<a xlink:href="javascript:alert(2)"><text>click2</text></a></svg>'
    );
    const html = injectedHtml().toLowerCase();
    expect(html).not.toContain('javascript:');
  });

  it('strips foreignObject and the HTML inside it', () => {
    renderSvg(
      '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject width="100" height="100">'
      + '<body xmlns="http://www.w3.org/1999/xhtml"><img src="x" onerror="alert(1)"/></body>'
      + '</foreignObject><rect width="5" height="5"/></svg>'
    );
    const html = injectedHtml().toLowerCase();
    expect(html).not.toContain('foreignobject');
    expect(html).not.toContain('onerror');
    expect(html).not.toContain('<img');
  });

  it('strips event handlers on shapes and animation elements that set handlers', () => {
    renderSvg(
      '<svg xmlns="http://www.w3.org/2000/svg">'
      + '<rect width="10" height="10" onclick="alert(1)"/>'
      + '<circle r="3" onmouseover="alert(2)"/>'
      + '<set attributeName="onload" to="alert(3)"/>'
      + '</svg>'
    );
    const html = injectedHtml().toLowerCase();
    expect(html).not.toContain('onclick');
    expect(html).not.toContain('onmouseover');
    expect(html).not.toContain('alert');
  });

  it('keeps a benign SVG intact: shapes, fills, gradients and text', () => {
    renderSvg(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
      + '<defs><linearGradient id="g1"><stop offset="0%" stop-color="#f00"/>'
      + '<stop offset="100%" stop-color="#00f"/></linearGradient></defs>'
      + '<rect x="1" y="2" width="40" height="30" fill="url(#g1)"/>'
      + '<circle cx="60" cy="40" r="12" fill="#0a0"/>'
      + '<text x="10" y="90" fill="#fff">Hello agent</text></svg>'
    );
    const wrap = document.querySelector('.scratchpad-svg');
    expect(wrap.querySelector('svg')).toBeTruthy();
    expect(wrap.querySelector('rect')).toBeTruthy();
    expect(wrap.querySelector('circle')).toBeTruthy();
    expect(wrap.querySelector('linearGradient, lineargradient')).toBeTruthy();
    expect(wrap.querySelector('text')?.textContent).toContain('Hello agent');
  });
});

describe('sanitizeSvg helper (S3-M1)', () => {
  it('returns an empty string for non-string input', () => {
    expect(sanitizeSvg(null)).toBe('');
    expect(sanitizeSvg(undefined)).toBe('');
    expect(sanitizeSvg(42)).toBe('');
  });

  it('removes script / handlers / javascript: URLs while preserving drawing elements', () => {
    const out = sanitizeSvg(
      '<svg onload="alert(1)"><script>alert(2)</script>'
      + '<a href="javascript:alert(3)"><rect width="9" height="9" fill="#123456"/></a></svg>'
    ).toLowerCase();
    expect(out).not.toContain('<script');
    expect(out).not.toContain('onload');
    expect(out).not.toContain('javascript:');
    expect(out).toContain('<rect');
    expect(out).toContain('#123456');
  });
});
