"""TTSR-style stream rules (Epic 7, E7-02).

Named regex rules evaluated on streamed model output, in the
spirit of omp's Tool/Text Stream Rules: a rule can

- ``redact`` — replace every match in the chunk with
  ``«redacted:<rule-name>»`` before the chunk is yielded, or
- ``block`` — stop the stream with an error naming the rule
  (audited as TTSR_BLOCKED by the provider).

Rules come from ``UserConfig.ttsr_rules`` — a list of
``{"name": ..., "pattern": ..., "action": "redact"|"block"}``.
The default is empty: TTSR is off until the user configures rules.
Invalid rules (bad name, uncompilable regex, unknown action) are
refused when the config loads (see ``clean_ttsr_rules``) and again
defensively at compile time here.

Known limitation, stated plainly: rules see individual stream
chunks, not the reassembled response. A pattern split across a
chunk boundary can slip past. Rules are a seatbelt for the common
case (a model blurting a whole key in one chunk), not a proof.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("aether_engine.providers.ttsr")

VALID_ACTIONS = ("redact", "block")


@dataclass
class CompiledRule:
    name: str
    pattern: re.Pattern
    action: str


def clean_ttsr_rules(raw) -> list[dict]:
    """Validate raw config entries; return only well-formed rules.

    Used by UserConfig loading: a malformed rule is dropped with a
    warning rather than stored to explode (or silently no-op) later.
    """
    cleaned: list[dict] = []
    if not isinstance(raw, list):
        return cleaned
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning("TTSR: dropping non-dict rule entry: %r", entry)
            continue
        name = entry.get("name")
        pattern = entry.get("pattern")
        action = entry.get("action", "redact")
        if not isinstance(name, str) or not name.strip():
            logger.warning("TTSR: dropping rule with no name: %r", entry)
            continue
        if not isinstance(pattern, str) or not pattern:
            logger.warning("TTSR: dropping rule %r with no pattern", name)
            continue
        if action not in VALID_ACTIONS:
            logger.warning("TTSR: dropping rule %r with unknown action %r", name, action)
            continue
        try:
            re.compile(pattern)
        except re.error as e:
            logger.warning("TTSR: dropping rule %r — invalid regex: %s", name, e)
            continue
        cleaned.append({"name": name, "pattern": pattern, "action": action})
    return cleaned


class StreamRules:
    """Compiled rule set applied to streamed text chunks."""

    def __init__(self, rules: list[dict] | None):
        self.rules: list[CompiledRule] = []
        for entry in clean_ttsr_rules(rules or []):
            self.rules.append(
                CompiledRule(entry["name"], re.compile(entry["pattern"]), entry["action"])
            )

    @property
    def active(self) -> bool:
        return bool(self.rules)

    def apply(self, text: str) -> tuple[str, str | None]:
        """Apply all rules to one chunk.

        Returns ``(text, blocked_rule_name)`` — when a block rule
        matches, the name is returned and the caller stops the
        stream; redactions from earlier rules are already applied
        to the returned text (irrelevant when blocking, kept for
        symmetry/debugging).
        """
        if not text or not self.rules:
            return text, None
        out = text
        for rule in self.rules:
            if rule.pattern.search(out):
                if rule.action == "block":
                    return out, rule.name
                out = rule.pattern.sub(f"«redacted:{rule.name}»", out)
        return out, None
