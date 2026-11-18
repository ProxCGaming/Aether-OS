/** D4-9: the roll-up classifiers apply the owning panels' own
 * state rules to the same list payloads those panels consume. */
import { mcpIssueCount, pluginIssueCount, skillIssueCount } from '../capabilityIssues';

describe('mcpIssueCount (McpPanel\'s error rule)', () => {
  test('counts failed and error-carrying servers, including invalid entries', () => {
    const servers = {
      good: { status: 'connected', error: null },
      off: { status: 'disabled', error: null, enabled: false },
      broken: { status: 'failed', error: null },
      noisy: { status: 'connected', error: 'handshake warning' },
      invalid: { status: 'invalid', error: 'missing command' },
    };
    expect(mcpIssueCount(servers)).toBe(3);
  });

  test('an unanswered or malformed payload counts nothing', () => {
    expect(mcpIssueCount(undefined)).toBe(0);
    expect(mcpIssueCount(null)).toBe(0);
    expect(mcpIssueCount({})).toBe(0);
  });
});

describe('pluginIssueCount (the registry load error is one issue)', () => {
  test('present registry_load_error counts once; absent counts zero', () => {
    expect(pluginIssueCount({ plugins: [], registry_load_error: 'corrupt' })).toBe(1);
    expect(pluginIssueCount({ plugins: [{ name: 'x' }], registry_load_error: 'corrupt' })).toBe(1);
    expect(pluginIssueCount({ plugins: [], registry_load_error: null })).toBe(0);
    expect(pluginIssueCount(undefined)).toBe(0);
  });
});

describe('skillIssueCount (SkillsPanel\'s load_error rule)', () => {
  test('counts only load_error rows', () => {
    const skills = [
      { name: 'fine', state: 'ok', enabled: true },
      { name: 'off', state: 'ok', enabled: false },
      { name: 'broken-md', state: 'load_error', error: 'frontmatter' },
      { name: 'broken-json', state: 'load_error', error: 'json' },
    ];
    expect(skillIssueCount(skills)).toBe(2);
    expect(skillIssueCount([])).toBe(0);
    expect(skillIssueCount(undefined)).toBe(0);
  });
});
