// Jest stub for remark-gfm. The package is ESM-only and Jest runs
// this suite as CJS, so the real module cannot load here (same
// reason suites stub react-markdown itself). MarkdownRenderer's
// wiring — that it hands this plugin to ReactMarkdown — is pinned
// by MarkdownRenderer.test.jsx against this stub; the real plugin's
// rendering (GFM tables, task lists) was verified outside Jest with
// the installed packages when D3-16 was fixed.
module.exports = {
  __esModule: true,
  default: function remarkGfmStub() {},
};
