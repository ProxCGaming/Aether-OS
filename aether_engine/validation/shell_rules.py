"""Segment-wise shell safety rules (Epic 2, E2-07).

Replaces the first-token-only shell check. The old pre-flight looked
at ``command.split()[0]`` and nothing else, so ``ls; rm -rf build``
validated as "ls". Here the command is split into segments on shell
operators (quote-aware), every segment is examined, and a table of
named rules assigns deny / ask verdicts:

- **deny** — catastrophic and never legitimate from an agent: fork
  bombs, ``rm -rf`` against ``/`` / ``~`` / ``$HOME``, raw disk
  writes (``mkfs``, ``dd of=/dev/…``, ``> /dev/sd…``), shutdown family.
- **ask** — legitimate but consequential: ``rm`` with recursive+force
  flags anywhere, fetch-then-execute (``curl … | sh``), recursive
  permission changes on absolute paths.

Users may append their own regex patterns via config
(``extra_shell_deny_patterns`` / ``extra_shell_ask_patterns``); user
patterns are evaluated with the defaults, deny first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ShellRule:
    name: str
    pattern: re.Pattern
    verdict: str  # "deny" | "ask"


@dataclass(frozen=True)
class ShellVerdict:
    verdict: str  # "allow" | "ask" | "deny"
    rule: str | None = None
    segment: str | None = None


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.IGNORECASE)


DEFAULT_RULES: list[ShellRule] = [
    # ── deny ────────────────────────────────────────────────────────
    ShellRule("fork-bomb", _rx(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "deny"),
    ShellRule("rm-root", _rx(r"\brm\b[^;|&]*(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive\s+--force|--force\s+--recursive)[^;|&]*\s(/|/\*|~|\$HOME)(\s|$|;|&)"), "deny"),
    ShellRule("rm-no-preserve-root", _rx(r"\brm\b[^;|&]*--no-preserve-root"), "deny"),
    ShellRule("mkfs", _rx(r"\bmkfs(\.\w+)?\b"), "deny"),
    ShellRule("dd-to-device", _rx(r"\bdd\b[^;|&]*\bof=/dev/"), "deny"),
    ShellRule("redirect-to-device", _rx(r">\s*/dev/(sd|hd|nvme|disk)"), "deny"),
    ShellRule("shutdown", _rx(r"\b(shutdown|reboot|poweroff|halt)\b"), "deny"),
    # ── ask ─────────────────────────────────────────────────────────
    ShellRule("fetch-execute", _rx(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "ask"),
    ShellRule("rm-recursive-force", _rx(r"\brm\b[^;|&]*(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive\s+--force|--force\s+--recursive)"), "ask"),
    ShellRule("recursive-chmod-system", _rx(r"\b(chmod|chown)\b[^;|&]*-[a-z]*R[a-z]*[^;|&]*\s/(etc|usr|var|bin|sbin|System|Library)\b"), "ask"),
]


def split_segments(command: str) -> list[str]:
    """Split a compound command on ; & | && || and newlines, quote-aware.

    Separators inside single or double quotes do not split, so
    ``echo "a; rm -rf x"`` stays one benign segment.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";&|\n":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _mask_quotes(text: str) -> str:
    """Blank out quoted string contents (quotes themselves are kept).

    The rules judge command STRUCTURE, not string literals:
    ``echo "rm -rf / is dangerous"`` must not trip the rm rule just
    because the words appear inside a quoted sentence.
    """
    out: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append(" ")
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def evaluate_shell_command(
    command: str,
    *,
    extra_deny: list[str] | None = None,
    extra_ask: list[str] | None = None,
) -> ShellVerdict:
    """Verdict for a whole command: worst rule hit wins (deny > ask)."""
    rules: list[ShellRule] = list(DEFAULT_RULES)
    for idx, pattern in enumerate(extra_deny or []):
        rules.append(ShellRule(f"user-deny-{idx}", _rx(pattern), "deny"))
    for idx, pattern in enumerate(extra_ask or []):
        rules.append(ShellRule(f"user-ask-{idx}", _rx(pattern), "ask"))

    # Whole-command pass first (catches patterns spanning substitutions
    # and pipes), then per-segment so the report names the segment.
    # Quoted literals are masked in both passes.
    targets = [_mask_quotes(command), *(_mask_quotes(s) for s in split_segments(command))]
    best = ShellVerdict("allow")
    for rule in rules:
        for target in targets:
            if rule.pattern.search(target):
                if rule.verdict == "deny":
                    return ShellVerdict("deny", rule=rule.name, segment=target.strip()[:120])
                if best.verdict == "allow":
                    best = ShellVerdict("ask", rule=rule.name, segment=target.strip()[:120])
    return best
