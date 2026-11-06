"""Deslop C3 — secret-redaction parity fixture (engine side).

The rule "which strings are secrets" is implemented twice:
``aether_engine/audit.py`` (``_SECRET_PATTERNS`` +
``_URL_CREDENTIAL_PATTERN``) and ``desktop_app/electron/appLog.cjs``
(``SECRET_PATTERNS`` + ``URL_CREDENTIAL_PATTERN``). A pattern added
or edited on one side only is a silent leak through the other side's
diagnostics bundles, so both suites assert their runtime list
against the one canonical fixture
(``tests/fixtures/secret_redaction_patterns.json``); the Jest twin
is ``desktop_app/electron/__tests__/appLogParity.test.js``.

Pattern sources are stored in the fixture in one canonical dialect;
``_canonical`` normalises the runtime sources into it. The rules
(also recorded in the fixture): ``\\/`` -> ``/`` (JS slash escaping),
``\\-]`` -> ``-]`` (literal hyphen at a character class's end,
escaped in the Python sources), and ``\\b(`` -> ``(\\b`` (the URL
pattern's word-boundary anchor sits outside the capture group in
JS, inside it in Python — the group captures the same text either
way). List order is asserted, not just membership: the PEM-block
pattern must run before the header-only one.
"""

import json
import re
from pathlib import Path

from aether_engine import audit

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "secret_redaction_patterns.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _join(value):
    # Fixture samples containing a PEM header are stored as fragment
    # arrays (split inside the header, so no committed line holds a
    # full header for secret scanners to trip on); join before use.
    return "".join(value) if isinstance(value, list) else value


def _canonical(source: str) -> str:
    return (
        source.replace("\\/", "/")
        .replace("\\-]", "-]")
        .replace("\\b(", "(\\b")
    )


def _entries() -> list[tuple[dict, re.Pattern]]:
    pairs = list(zip(FIXTURE["secret_patterns"], audit._SECRET_PATTERNS))
    pairs.append((FIXTURE["url_credential_pattern"], audit._URL_CREDENTIAL_PATTERN))
    return pairs


def test_secret_pattern_list_matches_fixture():
    runtime = [_canonical(p.pattern) for p in audit._SECRET_PATTERNS]
    assert runtime == [e["source"] for e in FIXTURE["secret_patterns"]]
    assert _canonical(audit._URL_CREDENTIAL_PATTERN.pattern) == (
        FIXTURE["url_credential_pattern"]["source"]
    )
    assert audit._REDACTED == FIXTURE["redacted_marker"]


def test_each_pattern_agrees_with_fixture_samples():
    for entry, runtime_pattern in _entries():
        canonical_pattern = re.compile(entry["source"])
        for sample in entry["must_match"]:
            sample = _join(sample)
            assert runtime_pattern.search(sample), (entry["id"], sample)
            assert canonical_pattern.search(sample), (entry["id"], sample)
        for sample in entry["must_not_match"]:
            sample = _join(sample)
            assert not runtime_pattern.search(sample), (entry["id"], sample)
            assert not canonical_pattern.search(sample), (entry["id"], sample)


def test_scrub_cases_match_fixture():
    for case in FIXTURE["scrub_cases"]:
        assert audit._scrub_string(_join(case["input"])) == _join(case["expected"])
