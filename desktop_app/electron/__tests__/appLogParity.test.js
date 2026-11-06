/**
 * Deslop C3 — secret-redaction parity fixture (desktop side).
 *
 * The rule "which strings are secrets" is implemented twice:
 * desktop_app/electron/appLog.cjs (SECRET_PATTERNS +
 * URL_CREDENTIAL_PATTERN) and aether_engine/audit.py
 * (_SECRET_PATTERNS + _URL_CREDENTIAL_PATTERN). A pattern added or
 * edited on one side only is a silent leak through the other side's
 * diagnostics bundles, so both suites assert their runtime list
 * against the one canonical fixture
 * (tests/fixtures/secret_redaction_patterns.json); the pytest twin
 * is tests/test_secret_redaction_parity.py.
 *
 * Pattern sources are stored in the fixture in one canonical
 * dialect; canonical() below normalises the runtime sources into
 * it. The rules (also recorded in the fixture): '\/' -> '/' (JS
 * slash escaping), '\-]' -> '-]' (literal hyphen at a character
 * class's end, escaped in the Python sources), and '\b(' -> '(\b'
 * (the URL pattern's word-boundary anchor sits outside the capture
 * group here, inside it in Python — the group captures the same
 * text either way). List order is asserted, not just membership:
 * the PEM-block pattern must run before the header-only one.
 */
const fs = require('fs');
const path = require('path');

const { scrubSecrets, SECRET_PATTERNS, URL_CREDENTIAL_PATTERN } = require('../appLog.cjs');

const FIXTURE_PATH = path.join(
  __dirname, '..', '..', '..', 'tests', 'fixtures', 'secret_redaction_patterns.json',
);
const FIXTURE = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf8'));

// Fixture samples containing a PEM header are stored as fragment
// arrays (split inside the header, so no committed line holds a full
// header for secret scanners to trip on); join before use.
function join(value) {
  return Array.isArray(value) ? value.join('') : value;
}

function canonical(source) {
  return source
    .replaceAll('\\/', '/')
    .replaceAll('\\-]', '-]')
    .replaceAll('\\b(', '(\\b');
}

function entries() {
  const pairs = FIXTURE.secret_patterns.map((entry, i) => [entry, SECRET_PATTERNS[i]]);
  pairs.push([FIXTURE.url_credential_pattern, URL_CREDENTIAL_PATTERN]);
  return pairs;
}

describe('secret-redaction parity with the engine audit redactor', () => {
  it('runtime pattern list matches the canonical fixture exactly', () => {
    expect(SECRET_PATTERNS.map((p) => canonical(p.source)))
      .toEqual(FIXTURE.secret_patterns.map((e) => e.source));
    expect(canonical(URL_CREDENTIAL_PATTERN.source))
      .toBe(FIXTURE.url_credential_pattern.source);
  });

  it('each pattern agrees with the fixture samples', () => {
    for (const [entry, runtimePattern] of entries()) {
      // Fresh RegExp from the source: no /g lastIndex state, and the
      // fixture's canonical pattern is compiled in this engine too.
      const runtime = new RegExp(runtimePattern.source);
      const canonicalPattern = new RegExp(entry.source);
      for (const rawSample of entry.must_match) {
        const sample = join(rawSample);
        expect(runtime.test(sample)).toBe(true);
        expect(canonicalPattern.test(sample)).toBe(true);
      }
      for (const rawSample of entry.must_not_match) {
        const sample = join(rawSample);
        expect(runtime.test(sample)).toBe(false);
        expect(canonicalPattern.test(sample)).toBe(false);
      }
    }
  });

  it('scrub cases match the fixture', () => {
    for (const scrubCase of FIXTURE.scrub_cases) {
      expect(scrubSecrets(join(scrubCase.input))).toBe(join(scrubCase.expected));
    }
  });
});
