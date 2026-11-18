import fs from 'fs';
import path from 'path';

// Full Furnish S-1 — re-solidify the shell, pinned by direct reads
// of the shipped stylesheets and consumer files (CSS has no runtime
// test surface in this repo; definition + consumer greps are the
// strongest honest check, as in Wave3CssDedup):
//   - The glass RECIPE is untouched: .aether-glass / .ios-glass in
//     chat.css and the --glass-* tokens in index.css keep their
//     exact Wave 6 values. S-1 changes WHERE glass lives, never
//     what it is.
//   - The non-Chat shell panel (App.jsx) and the docked Artifacts
//     canvas use the solid .aether-content-panel; permanent-list
//     buttons/cards use .aether-solid-btn / .aether-solid-card.
//   - .ios-glass-input is solid under its existing name (every
//     consumer is a permanent input), with its focus ring intact.
//   - .ios-close-btn is gone: its last consumers (Settings close,
//     transcript approval buttons) are solid, and grep proved no
//     other consumers exist.
//   - Floating layers keep the recipe: chat window root, dropdown
//     menu, command palette, voice overlay.

const read = (...parts) => fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');

const chatCss = read('chat.css');
const indexCss = read('index.css');
const appJsx = read('App.jsx');
const artifactsCanvas = read('components', 'ArtifactsCanvas.jsx');
const downloadedModels = read('components', 'DownloadedModels.jsx');
const ollamaModelList = read('components', 'OllamaModelList.jsx');
const localModelsTab = read('LocalModelsTab.jsx');
const chatTranscript = read('components', 'ChatTranscript.jsx');
const chatWindow = read('DraggableChatWindow.jsx');
const dropdown = read('Dropdown.jsx');
const commandPalette = read('components', 'CommandPalette.jsx');
const voiceOverlay = read('components', 'VoiceOverlay.jsx');
const mcpPanel = read('components', 'Settings', 'McpPanel.jsx');

const RECIPE = 'blur(16px) saturate(140%) url(#aether-glass-refraction)';

const ruleCount = (css, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return (css.match(new RegExp(`^${escaped}\\s*\\{`, 'gm')) || []).length;
};

const block = (css, selector) => {
  const start = css.indexOf(`${selector} {`);
  expect(start).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf('}', start));
};

describe('S-1 — the glass recipe itself is unchanged', () => {
  it('.aether-glass and .ios-glass keep the exact Wave 6 recipe', () => {
    for (const sel of ['.aether-glass', '.ios-glass']) {
      const b = block(chatCss, sel);
      expect(b).toContain(`backdrop-filter: ${RECIPE};`);
      expect(b).toContain('border: 1px solid var(--glass-edge);');
      expect(b).toContain('box-shadow: inset 0 1px 0 var(--glass-specular), 0 16px 40px rgba(0, 0, 0, 0.45);');
    }
  });

  it('the --glass-* tokens keep their approved values', () => {
    expect(indexCss).toContain('--glass-fill: rgba(255, 255, 255, 0.07);');
    expect(indexCss).toContain('--glass-edge: rgba(255, 255, 255, 0.22);');
    expect(indexCss).toContain('--glass-specular: rgba(255, 255, 255, 0.45);');
  });
});

describe('S-1 — the solid shell family exists and carries no blur', () => {
  it('.aether-content-panel is defined once, solid, in index.css', () => {
    expect(ruleCount(indexCss, '.aether-content-panel')).toBe(1);
    const b = block(indexCss, '.aether-content-panel');
    expect(b).toContain('background: var(--bg-raised);');
    expect(b).toContain('border: 1px solid var(--border-default);');
    expect(b).not.toContain('backdrop-filter');
  });

  it('.aether-solid-card and .aether-solid-btn are defined once, solid', () => {
    expect(ruleCount(indexCss, '.aether-solid-card')).toBe(1);
    expect(ruleCount(indexCss, '.aether-solid-btn')).toBe(1);
    expect(block(indexCss, '.aether-solid-card')).not.toContain('backdrop-filter');
    expect(block(indexCss, '.aether-solid-btn')).not.toContain('backdrop-filter');
  });
});

describe('S-1 — permanent surfaces are restyled', () => {
  it('the App.jsx non-Chat panel is the solid panel, not .ios-glass', () => {
    expect(appJsx).toContain('className="aether-content-panel"');
    expect(appJsx).not.toContain('className="ios-glass"');
  });

  it('the docked Artifacts canvas is solid — no glass class, no inline recipe', () => {
    expect(artifactsCanvas).toContain('className="aether-content-panel"');
    expect(artifactsCanvas).not.toContain('backdropFilter');
    expect(artifactsCanvas).not.toContain('ios-glass');
  });

  it('model-list buttons and cards carry no .ios-glass', () => {
    for (const src of [downloadedModels, ollamaModelList, localModelsTab]) {
      expect(src).not.toContain('className="ios-glass"');
    }
    expect(localModelsTab).toContain('className="aether-solid-card"');
    expect(downloadedModels).toContain('aether-solid-btn');
  });

  it('.ios-glass-input is solid, with its focus ring preserved exactly', () => {
    const b = block(chatCss, '.ios-glass-input');
    expect(b).not.toContain('backdrop-filter');
    expect(b).toContain('background-color: var(--bg-base);');
    expect(b).toContain('border: 1px solid var(--border-default);');
    const focus = block(chatCss, '.ios-glass-input:focus-within');
    expect(focus).not.toContain('backdrop-filter');
    expect(focus).toContain('border-color: var(--accent);');
    expect(focus).toContain('box-shadow: 0 0 0 3px rgba(var(--accent-rgb), 0.18);');
  });

  it('Settings inputs restyle through the shared class — consumer names unchanged', () => {
    // McpPanel (Lane 4) was NOT edited: its fields still name
    // .ios-glass-input, and the solid definition above is what
    // restyles them (MERGE-B's single coordinated change).
    expect(mcpPanel).toContain('ios-glass-input');
  });

  it('.ios-close-btn is removed and unreferenced', () => {
    expect(ruleCount(chatCss, '.ios-close-btn')).toBe(0);
    expect(appJsx).not.toContain('ios-close-btn');
    expect(chatTranscript).not.toContain('ios-close-btn');
    expect(chatTranscript).toContain('aether-solid-btn');
  });
});

describe('S-1 — floating layers keep the glass recipe', () => {
  it('the chat window root and the dropdown menu still use .ios-glass', () => {
    expect(chatWindow).toContain('className="ios-glass"');
    expect(dropdown).toContain('className="ios-glass"');
    expect(ruleCount(chatCss, '.ios-glass')).toBe(1);
  });

  it('palette and voice overlay keep their inline recipe', () => {
    expect(commandPalette).toContain(RECIPE);
    expect(voiceOverlay).toContain(RECIPE);
  });
});
