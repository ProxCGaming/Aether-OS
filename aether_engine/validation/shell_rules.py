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

Masking quoted literals keeps ``echo "rm -rf / is dangerous"`` from
false-positiving, but masking alone hid denied payloads handed to a
nested shell (S2-M6): ``bash -c "rm -rf /"`` masked to
``bash -c "        "``. So quoted text is judged by the position it
occupies: a quoted string that a carrier will EXECUTE — a
``bash``/``sh``/… ``-c`` payload, an ``eval`` argument, a command or
process substitution — is extracted and recursively evaluated as a
command line of its own, while quoted data passed to ordinary
commands stays masked and inert.
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
    because the words appear inside a quoted sentence. Escape-aware:
    ``\\"`` inside double quotes is a literal quote, not the end of
    the string, and a backslash-escaped quote outside quotes does not
    open one. Length is preserved so positions stay aligned.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote == "'":
            # No escapes exist inside single quotes.
            if ch == "'":
                quote = None
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < len(text):
                out.append("  ")
                i += 2
                continue
            if ch == '"':
                quote = None
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


# ── Nested-command extraction (S2-M6) ────────────────────────────

_NESTED_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"}

# Commands whose following words are another command line: wrappers
# are skipped (with their flags) to find the real command word.
_WRAPPER_COMMANDS = {
    "sudo", "doas", "env", "command", "builtin", "exec", "nice",
    "timeout", "stdbuf", "nohup", "setsid", "xargs", "runuser",
    "pkexec", "time",
}

# Wrapper flags that consume the next token as their argument.
_FLAGS_WITH_ARG = {
    "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
    "-a", "--name", "-n", "--adjustment", "-o", "-e", "-s", "--signal",
    "-k", "--kill-after", "-t", "-d", "--delimiter", "-E", "-I", "-L",
    "-P", "-J", "-S", "--unset", "-C", "--chdir",
}

_MAX_NESTING = 6


@dataclass(frozen=True)
class _Token:
    value: str  # decoded: quotes removed, escapes resolved
    start: int
    end: int


def _lex(segment: str) -> list[_Token]:
    """Split one segment into tokens, decoding quotes and escapes.

    ``bash -c "rm -rf /"`` lexes to ``bash``, ``-c``, ``rm -rf /`` —
    the payload arrives as ONE token whose value is the inner command
    line, which is exactly what makes it recognisable as code rather
    than data.
    """
    tokens: list[_Token] = []
    i, n = 0, len(segment)
    while i < n:
        if segment[i] in " \t":
            i += 1
            continue
        start = i
        parts: list[str] = []
        while i < n and segment[i] not in " \t":
            ch = segment[i]
            if ch == "'":
                j = segment.find("'", i + 1)
                if j == -1:
                    parts.append(segment[i + 1:])
                    i = n
                else:
                    parts.append(segment[i + 1:j])
                    i = j + 1
            elif ch == '"':
                i += 1
                while i < n and segment[i] != '"':
                    if segment[i] == "\\" and i + 1 < n and segment[i + 1] in '"\\$`':
                        parts.append(segment[i + 1])
                        i += 2
                    else:
                        parts.append(segment[i])
                        i += 1
                i += 1  # closing quote (or one past EOF)
            elif ch == "\\" and i + 1 < n:
                parts.append(segment[i + 1])
                i += 2
            else:
                parts.append(ch)
                i += 1
        tokens.append(_Token("".join(parts), start, i))
    return tokens


def _basename(value: str) -> str:
    return value.lstrip("({!").rsplit("/", 1)[-1]


def _skip_to_command_word(tokens: list[_Token], pos: int) -> int:
    """Advance past wrappers (``sudo``, ``env``, ``timeout 5`` …),
    their flags, and ``VAR=value`` assignments to the command word."""
    while pos < len(tokens):
        value = tokens[pos].value
        if value and all(c == "(" for c in value):
            pos += 1
            continue
        base = _basename(value)
        if base in _WRAPPER_COMMANDS:
            pos += 1
            is_timeout = base == "timeout"
            while pos < len(tokens) and tokens[pos].value.startswith("-") and tokens[pos].value not in ("-", "--"):
                flag = tokens[pos].value
                pos += 1
                if "=" not in flag and flag in _FLAGS_WITH_ARG and pos < len(tokens):
                    pos += 1
            if is_timeout and pos < len(tokens) and re.fullmatch(r"\d+(?:\.\d+)?[smhdw]?", tokens[pos].value):
                pos += 1
            continue
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", value):
            pos += 1
            continue
        break
    return pos


def _command_positions(tokens: list[_Token]) -> list[int]:
    """Token indexes that sit in command position in a segment: the
    segment's own command word, plus the command word of any
    ``find … -exec <cmd>`` clause. An ``echo bash -c …`` has ``bash``
    as mere argument data — it is NOT in this list."""
    positions: list[int] = []
    first = _skip_to_command_word(tokens, 0)
    if first < len(tokens):
        positions.append(first)
    for i, tok in enumerate(tokens):
        if tok.value in ("-exec", "-execdir") and i + 1 < len(tokens):
            pos = _skip_to_command_word(tokens, i + 1)
            if pos < len(tokens) and pos not in positions:
                positions.append(pos)
    return positions


def _segment_payloads(segment: str) -> list[str]:
    """Command lines a segment asks a carrier to execute: the payload
    of a nested shell's ``-c`` (``bash``/``sh``/…/``su``) in command
    position, and the argument string of ``eval``."""
    tokens = _lex(segment)
    payloads: list[str] = []
    for idx in _command_positions(tokens):
        base = _basename(tokens[idx].value)
        if base == "eval":
            rest = tokens[idx + 1:]
            if rest:
                payloads.append(" ".join(t.value for t in rest))
            continue
        if base not in _NESTED_SHELLS and base != "su":
            continue
        # Shell options precede the operand: scan flag tokens for a
        # cluster containing ``c``; its payload is glued to the flag
        # (``-crm …``) or is the next token (``-c "…"``, ``-lc "…"``).
        j = idx + 1
        nonflags = 0
        while j < len(tokens):
            flag = tokens[j].value
            if flag == "--":
                break
            if flag.startswith("-") and flag != "-":
                if flag.startswith("--"):
                    if flag == "--command" and j + 1 < len(tokens):
                        payloads.append(tokens[j + 1].value)
                    elif flag.startswith("--command="):
                        payloads.append(flag[len("--command="):])
                    else:
                        j += 1
                        continue
                    break
                cluster = flag[1:]
                if "c" in cluster:
                    cpos = cluster.index("c")
                    if cpos < len(cluster) - 1:
                        payloads.append(flag[cpos + 2:])
                    elif j + 1 < len(tokens):
                        payloads.append(tokens[j + 1].value)
                    break
                j += 1
                continue
            if base == "su" and nonflags < 2:  # su [user] -c "…"
                nonflags += 1
                j += 1
                continue
            break
    return [p for p in payloads if p.strip()]


def _find_closing_paren(text: str, open_idx: int) -> int | None:
    depth = 0
    quote: str | None = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote != "'":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _extract_substitutions(text: str) -> list[str]:
    """Contents of ``$(…)``, `` `…` `` and ``<(…)``/``>(…)`` — code the
    shell executes even inside double quotes (single quotes suppress
    them, and the scan honours that). ``$((…))`` arithmetic is not a
    command and is skipped."""
    payloads: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
        else:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                quote = "'"
                i += 1
                continue
            if ch == '"':
                quote = '"'
                i += 1
                continue
        # Unquoted or double-quoted: substitutions are live here.
        if ch == "`":
            j = i + 1
            while j < len(text):
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "`":
                    break
                j += 1
            if j < len(text):
                payloads.append(text[i + 1:j])
                i = j + 1
                continue
            i += 1
            continue
        if ch == "$" and text.startswith("$((", i):
            end = _find_closing_paren(text, i + 1)
            i = end + 1 if end is not None else i + 2
            continue
        if ch == "$" and i + 1 < len(text) and text[i + 1] == "(":
            end = _find_closing_paren(text, i + 1)
            if end is not None:
                payloads.append(text[i + 2:end])
                i = end + 1
                continue
        if ch in "<>" and i + 1 < len(text) and text[i + 1] == "(":
            end = _find_closing_paren(text, i + 1)
            if end is not None:
                payloads.append(text[i + 2:end])
                i = end + 1
                continue
        i += 1
    return payloads


def _nested_payloads(command: str) -> list[str]:
    payloads = _extract_substitutions(command)
    for segment in split_segments(command):
        payloads.extend(_segment_payloads(segment))
    return payloads


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

    return _evaluate(command, rules, depth=0)


def _evaluate(command: str, rules: list[ShellRule], *, depth: int) -> ShellVerdict:
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
    # Command-position pass (S2-M6): quoted strings a carrier will
    # execute (``bash -c`` payloads, ``eval`` arguments, command
    # substitutions) are re-evaluated as command lines of their own —
    # with the same masked rules, so quoted data inside THEM is still
    # inert. Quoted arguments to ordinary commands never reach here.
    if depth < _MAX_NESTING:
        for payload in _nested_payloads(command):
            sub = _evaluate(payload, rules, depth=depth + 1)
            if sub.verdict == "deny":
                return sub
            if sub.verdict == "ask" and best.verdict == "allow":
                best = sub
    return best
