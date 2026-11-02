/** Commit message rules for Aether-OS (issue 001).
 * Enforces Conventional Commits with the 9 unified types agreed in the
 * 27 Oct 2026 grill (Q3): feat, fix, test, refactor, docs, chore, perf,
 * ci, style. Scope is optional. The whole message must be at least
 * 20 characters, matching the previous custom checker.
 * No maximum header length, and no body/footer line-length limit —
 * the previous checker had neither, and the grill decided no such
 * rule. (config-conventional defaults both to 100; disabling them
 * keeps only the decided rules blocking.)
 */
module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum': [
      2,
      'always',
      ['feat', 'fix', 'test', 'refactor', 'docs', 'chore', 'perf', 'ci', 'style'],
    ],
    'header-min-length': [2, 'always', 20],
    'header-max-length': [0],
    'body-max-line-length': [0],
    'footer-max-line-length': [0],
  },
};
