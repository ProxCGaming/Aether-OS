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

describe('Wave 4 — desktop dead code sweep (D-07 + full-surface grep proof)', () => {
  // Proof standard: git grep over every tracked file in the repo
  // (JSX/JS sources, inline style strings, electron main/preload,
  // tests, index.html, vite/build config). A name that occurs
  // nowhere but its own definition is dead. Names that survive
  // only inside dynamic constructions count as live and stay.
  it('deletes App.css — the unimported Vite starter template (design D-07)', () => {
    expect(fs.existsSync(path.join(__dirname, '..', 'App.css'))).toBe(false);
  });

  it('drops the pre-"Upgraded" tool card rules no JSX carries', () => {
    for (const sel of ['.tool-header', '.tool-name-container', '.tool-icon',
      '.tool-args', '.tool-result', '.tool-show-more-btn']) {
      expect(ruleCount(chatCss, sel)).toBe(0);
    }
    expect(chatCss).not.toContain('.tool-result::-webkit-scrollbar');
    expect(chatCss).not.toContain('.tool-args::-webkit-scrollbar');
  });

  it('drops the --bg-color token nothing references', () => {
    expect(indexCss).not.toContain('--bg-color:');
  });

  it('keeps the live keyframes — same-stylesheet and inline-style consumers', () => {
    // Referenced by animation: declarations inside chat.css itself.
    for (const kf of ['pulsePending', 'pulseBadge', 'pulseStop',
      'pulseDot', 'contextMenuFadeIn', 'toastFadeIn']) {
      expect(chatCss).toContain(`@keyframes ${kf}`);
    }
    // float1/2/3 drive the App.jsx orbs via inline style strings.
    for (const kf of ['float1', 'float2', 'float3',
      'voiceWavePulse', 'voiceDotPulse']) {
      expect(indexCss).toContain(`@keyframes ${kf}`);
    }
  });

  it('keeps the tokens backing live rules (--panel-* for .glass-panel)', () => {
    expect(indexCss).toContain('--panel-bg:');
    expect(indexCss).toContain('--panel-border:');
    expect(indexCss).toContain('--text-primary:');
    expect(indexCss).toContain('--text-secondary:');
  });
});

describe('Wave 5 — .spin-animation is defined (used-but-undefined class)', () => {
  // Observed in Wave 4 and closed here: LocalModelsTab renders
  // <RefreshCw className="spin-animation" /> at 3 sites (download
  // progress, searching, inventory loading), but no stylesheet
  // defined the class, so those icons never spun. The rule drives
  // the live @keyframes spin in the same stylesheet.
  it('is defined exactly once, in index.css beside @keyframes spin', () => {
    expect(ruleCount(indexCss, '.spin-animation')).toBe(1);
    expect(ruleCount(chatCss, '.spin-animation')).toBe(0);
    const start = indexCss.indexOf('.spin-animation {');
    const block = indexCss.slice(start, indexCss.indexOf('}', start));
    expect(block).toContain('animation: spin 1s linear infinite');
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
