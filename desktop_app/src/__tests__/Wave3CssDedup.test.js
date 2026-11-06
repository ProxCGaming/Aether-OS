import fs from 'fs';
import path from 'path';

// Wave 3 (D3 chunk) — stylesheet dedup, pinned by direct reads of
// the two shipped stylesheets (CSS has no runtime test surface in
// this repo; a definition count is the strongest honest check):
//   D3-08 — .tool-activity-card defined ONCE in chat.css (the
//           "upgraded" solid block rules; the glass block — and its
//           backdrop-filter — is gone), and the .tool-status-*
//           variants live only in their .tool-status-pill form,
//           keeping the pending pulse animation.
//   D3-09 — .session-item.active defined ONCE, in chat.css beside
//           the .session-item base rule it extends.
//   D3-20 — proven-dead CSS removed: the unreferenced
//           --accent-hover token and the unused .draggable-handle
//           classes. (@keyframes spin was a candidate but is NOT
//           dead — inline styles in DraggableChatWindow.jsx and
//           SettingsPanel.jsx animate with `spin 1s linear
//           infinite` — so it stays, pinned by a test below.)

const chatCss = fs.readFileSync(path.join(__dirname, '..', 'chat.css'), 'utf8');
const indexCss = fs.readFileSync(path.join(__dirname, '..', 'index.css'), 'utf8');

const ruleCount = (css, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return (css.match(new RegExp(`^${escaped}\\s*\\{`, 'gm')) || []).length;
};

describe('D3-08 — tool activity card and status pills have single definitions', () => {
  it('.tool-activity-card is defined exactly once in chat.css', () => {
    expect(ruleCount(chatCss, '.tool-activity-card')).toBe(1);
  });

  it('the surviving card definition is the solid "upgraded" one — no glass blur', () => {
    expect(chatCss).not.toContain('blur(20px) saturate(200%)');
    const start = chatCss.indexOf('.tool-activity-card {');
    const block = chatCss.slice(start, chatCss.indexOf('}', start));
    expect(block).toContain('background: rgba(15, 15, 26, 0.7)');
    expect(block).not.toContain('backdrop-filter');
  });

  it('.tool-status-* variants exist only in .tool-status-pill form, pulse intact', () => {
    expect(ruleCount(chatCss, '.tool-status-pending')).toBe(0);
    expect(ruleCount(chatCss, '.tool-status-completed')).toBe(0);
    expect(ruleCount(chatCss, '.tool-status-failed')).toBe(0);
    expect(ruleCount(chatCss, '.tool-status')).toBe(0);
    const start = chatCss.indexOf('.tool-status-pill.tool-status-pending {');
    const block = chatCss.slice(start, chatCss.indexOf('}', start));
    expect(block).toContain('animation: pulsePending');
  });
});

describe('D3-09 — .session-item.active has one owner', () => {
  it('is absent from index.css and defined once in chat.css', () => {
    expect(ruleCount(indexCss, '.session-item.active')).toBe(0);
    expect(ruleCount(chatCss, '.session-item.active')).toBe(1);
  });
});

describe('D3-20 — proven-dead CSS is removed', () => {
  it('keeps @keyframes spin — inline JSX styles animate with it', () => {
    expect(/@keyframes spin\s*\{/.test(indexCss)).toBe(true);
    expect(indexCss).toContain('@keyframes aether-spin');
  });

  it('drops the unreferenced --accent-hover token', () => {
    expect(indexCss).not.toContain('--accent-hover:');
    expect(chatCss).not.toContain('--accent-hover:');
  });

  it('drops the unused .draggable-handle classes', () => {
    expect(ruleCount(chatCss, '.draggable-handle')).toBe(0);
    expect(ruleCount(chatCss, '.draggable-handle:active')).toBe(0);
  });
});
