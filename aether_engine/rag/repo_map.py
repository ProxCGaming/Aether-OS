"""Repo map for graph tasks (Epic 8, E8-3.1; grill Q15/Q18).

Aider-style ambient code context: tree-sitter parses the granted
workspace's live files, extracts symbol *definitions* (signatures
only — bodies never enter the map) and cross-file *references*, builds
a defs/refs graph over files, and ranks files with PageRank
personalised to the task — files named in the prompt and files
defining identifiers the prompt mentions get a strong teleport bias,
files in play in the conversation a smaller one. The rendered map is
truncated to a token budget (~1k by default, ``UserConfig``).

Freshness: the map is derived from the working tree on every call.
A small in-process cache keyed by each file's (mtime_ns, size)
signature skips re-parsing unchanged files, but an edit between
tasks always changes the signature and therefore the next map —
there is no manual invalidation step.

Graph tasks only (Q18): the supervisor injects this map; fast chat
never builds one. Nothing here is embedded or transmitted — the map
is computed locally from files the user already granted.
"""
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

logger = logging.getLogger("aether_engine.rag.repo_map")

# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------

_EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
}

_JS_LANGS = {"javascript", "typescript", "tsx"}

# Directories that are never source worth mapping: dependency folders,
# build output, VCS and tool caches. Root .gitignore rules apply on
# top of this (see _Gitignore).
_DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
    "venv", "dist", "build", "out", ".next", ".nuxt", "target",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea",
    ".aether",
}

_MAX_FILE_BYTES = 512_000
_MAX_FILES = 5_000
_PAGERANK_DAMPING = 0.85

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def estimate_tokens(text: str) -> int:
    """Rough token estimate (chars / 4, rounded up).

    The same estimator enforces the budget at render time and is what
    tests assert against — the map's contract is "within budget by
    this measure", matching Aider's approximate budgeting.
    """
    return (len(text) + 3) // 4


# ---------------------------------------------------------------------------
# Parsed model
# ---------------------------------------------------------------------------

@dataclass
class SymbolDef:
    name: str
    signature: str
    indent: int = 0  # 0 = top level, 1 = class member


@dataclass
class FileParse:
    relpath: str
    defs: list[SymbolDef] = field(default_factory=list)
    refs: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------------------
# .gitignore (minimal matcher: root file, globs, dir patterns, anchors;
# negations are not supported — a documented v1 limitation)
# ---------------------------------------------------------------------------

class _Gitignore:
    def __init__(self, root: Path):
        self._patterns: list[tuple[str, bool, bool]] = []  # (pat, dir_only, anchored)
        try:
            lines = (root / ".gitignore").read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            pat = line.strip()
            if not pat or pat.startswith(("#", "!")):
                continue
            dir_only = pat.endswith("/")
            pat = pat.rstrip("/")
            anchored = "/" in pat
            pat = pat.lstrip("/")
            if pat:
                self._patterns.append((pat, dir_only, anchored))

    def matches(self, rel_posix: str, *, is_dir: bool) -> bool:
        parts = rel_posix.split("/")
        for pat, dir_only, anchored in self._patterns:
            if dir_only and not is_dir:
                # A dir pattern still excludes everything under it.
                if not any(
                    self._match_one(pat, "/".join(parts[: i + 1]), anchored)
                    for i in range(len(parts) - 1)
                ):
                    continue
                return True
            if self._match_one(pat, rel_posix, anchored):
                return True
        return False

    @staticmethod
    def _match_one(pat: str, rel_posix: str, anchored: bool) -> bool:
        if anchored:
            return fnmatch(rel_posix, pat) or fnmatch(rel_posix, pat + "/*")
        return any(fnmatch(part, pat) for part in rel_posix.split("/")) or fnmatch(
            rel_posix, pat)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover(roots: list[str]) -> list[tuple[Path, str, str]]:
    """(abspath, relpath, lang) for every mappable file under *roots*."""
    found: list[tuple[Path, str, str]] = []
    multi = len(roots) > 1
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        ignore = _Gitignore(root_path)
        prefix = f"{root_path.name}/" if multi else ""
        for dirpath, dirnames, filenames in os.walk(root_path):
            rel_dir = Path(dirpath).relative_to(root_path)
            kept = []
            for d in dirnames:
                rel_d = (rel_dir / d).as_posix()
                if d in _DEFAULT_IGNORE_DIRS or ignore.matches(rel_d, is_dir=True):
                    continue
                kept.append(d)
            dirnames[:] = kept
            for name in filenames:
                lang = _EXT_TO_LANG.get(Path(name).suffix.lower())
                if lang is None:
                    continue
                rel = (rel_dir / name).as_posix()
                if ignore.matches(rel, is_dir=False):
                    continue
                abspath = Path(dirpath) / name
                try:
                    if abspath.stat().st_size > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                found.append((abspath, prefix + rel, lang))
                if len(found) >= _MAX_FILES:
                    return found
    return found


# ---------------------------------------------------------------------------
# Tree-sitter extraction
# ---------------------------------------------------------------------------

_parsers: dict[str, object] = {}


def _parser_for(lang: str):
    if lang in _parsers:
        return _parsers[lang]
    from tree_sitter import Language, Parser

    if lang == "python":
        import tree_sitter_python as grammar

        language = Language(grammar.language())
    elif lang == "javascript":
        import tree_sitter_javascript as grammar

        language = Language(grammar.language())
    elif lang == "typescript":
        import tree_sitter_typescript as grammar

        language = Language(grammar.language_typescript())
    else:  # tsx
        import tree_sitter_typescript as grammar

        language = Language(grammar.language_tsx())
    parser = Parser(language)
    _parsers[lang] = parser
    return parser


def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _signature(source: bytes, node) -> str:
    """The definition's header text: everything up to its body,
    whitespace-collapsed. Signatures only — bodies never render.

    The header ends at the child just before the body node (the ":"
    of a Python def, the parameter list of a JS function): comments
    sitting between the header and the first body statement belong
    to the body and must not leak into the signature.
    """
    body = node.child_by_field_name("body")
    end = node.end_byte
    if body is not None:
        end = body.start_byte
        for child in node.children:
            if child.id == body.id:
                break
            if child.type == "comment":
                continue
            end = child.end_byte
    return " ".join(source[node.start_byte:end].decode("utf-8", "replace").split())


def _unwrap_decorated(node):
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                return child
    return node


def _extract_python(root_node, source: bytes) -> tuple[list[SymbolDef], Counter]:
    defs: list[SymbolDef] = []
    for child in root_node.children:
        node = _unwrap_decorated(child)
        if node.type == "function_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                defs.append(SymbolDef(_text(source, name), _signature(source, node)))
        elif node.type == "class_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                defs.append(SymbolDef(_text(source, name), _signature(source, node)))
            body = node.child_by_field_name("body")
            if body is not None:
                for member in body.children:
                    m = _unwrap_decorated(member)
                    if m.type == "function_definition":
                        mname = m.child_by_field_name("name")
                        if mname is not None:
                            defs.append(SymbolDef(
                                _text(source, mname), _signature(source, m), indent=1))
    return defs, _collect_refs(root_node, source, "python")


def _js_def_from_declarator(source: bytes, decl_kind: str, node) -> SymbolDef | None:
    name_node = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    if name_node is None or value is None:
        return None
    if value.type not in ("arrow_function", "function_expression"):
        return None
    first_line = source[node.start_byte:node.end_byte].decode(
        "utf-8", "replace").splitlines()[0]
    sig = " ".join(first_line.split()).rstrip("{").rstrip()
    if decl_kind:
        sig = f"{decl_kind} {sig}" if not sig.startswith(decl_kind) else sig
    return SymbolDef(_text(source, name_node), sig)


def _extract_js(root_node, source: bytes) -> tuple[list[SymbolDef], Counter]:
    defs: list[SymbolDef] = []

    def add_class(node, indent=0):
        name = node.child_by_field_name("name")
        if name is not None:
            defs.append(SymbolDef(_text(source, name), _signature(source, node), indent))
        body = node.child_by_field_name("body")
        if body is not None:
            for member in body.children:
                if member.type in ("method_definition", "abstract_method_signature"):
                    mname = member.child_by_field_name("name")
                    if mname is not None:
                        defs.append(SymbolDef(
                            _text(source, mname), _signature(source, member),
                            indent=indent + 1))

    def visit(node, indent=0):
        if node.type == "export_statement":
            for child in node.children:
                visit(child, indent)
            return
        if node.type == "function_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                defs.append(SymbolDef(_text(source, name), _signature(source, node), indent))
        elif node.type in ("class_declaration", "abstract_class_declaration"):
            add_class(node, indent)
        elif node.type == "interface_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                defs.append(SymbolDef(_text(source, name), _signature(source, node), indent))
        elif node.type in ("lexical_declaration", "variable_declaration"):
            kind = _text(source, node.children[0]) if node.children else ""
            for child in node.children:
                if child.type == "variable_declarator":
                    d = _js_def_from_declarator(source, kind, child)
                    if d is not None:
                        defs.append(d)

    for child in root_node.children:
        visit(child)
    return defs, _collect_refs(root_node, source, "javascript")


def _collect_refs(root_node, source: bytes, lang: str) -> Counter:
    refs: Counter = Counter()
    stack = [root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if lang == "python":
            if node.type != "call":
                continue
            fn = node.child_by_field_name("function")
            if fn is None:
                continue
            if fn.type == "identifier":
                refs[_text(source, fn)] += 1
            elif fn.type == "attribute":
                attr = fn.child_by_field_name("attribute")
                if attr is not None:
                    refs[_text(source, attr)] += 1
        else:
            if node.type == "call_expression":
                fn = node.child_by_field_name("function")
            elif node.type == "new_expression":
                fn = node.child_by_field_name("constructor")
            else:
                continue
            if fn is None:
                continue
            if fn.type == "identifier":
                refs[_text(source, fn)] += 1
            elif fn.type == "member_expression":
                prop = fn.child_by_field_name("property")
                if prop is not None:
                    refs[_text(source, prop)] += 1
    return refs


def _parse_file(abspath: Path, relpath: str, lang: str) -> FileParse | None:
    try:
        source = abspath.read_bytes()
        tree = _parser_for(lang).parse(source)
    except Exception:  # one unparsable file must not sink the map
        logger.debug("Repo map: skipping unparsable file %s", relpath, exc_info=True)
        return None
    if lang == "python":
        defs, refs = _extract_python(tree.root_node, source)
    else:
        defs, refs = _extract_js(tree.root_node, source)
    return FileParse(relpath=relpath, defs=defs, refs=refs)


# ---------------------------------------------------------------------------
# Parse cache: keyed by (mtime_ns, size) signature over the whole tree,
# so an edit between tasks always produces a fresh parse (freshness
# criterion) while unchanged trees are served without re-parsing.
# ---------------------------------------------------------------------------

_parse_cache: dict[tuple[str, ...], tuple[tuple, dict[str, FileParse]]] = {}


def _load_files(roots: list[str]) -> dict[str, FileParse]:
    candidates = _discover(roots)
    signature_parts = []
    for abspath, relpath, _lang in candidates:
        try:
            st = abspath.stat()
            signature_parts.append((relpath, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    signature = tuple(sorted(signature_parts))
    key = tuple(sorted(roots))
    cached = _parse_cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    files: dict[str, FileParse] = {}
    for abspath, relpath, lang in candidates:
        parsed = _parse_file(abspath, relpath, lang)
        if parsed is not None:
            files[relpath] = parsed
    _parse_cache[key] = (signature, files)
    return files


# ---------------------------------------------------------------------------
# Ranking: PageRank over the defs/refs graph, personalised to the task
# ---------------------------------------------------------------------------

def _mentions(text: str, files: dict[str, FileParse]) -> tuple[set[str], set[str]]:
    """Files and defined identifiers literally mentioned in *text*."""
    mentioned_files: set[str] = set()
    mentioned_idents: set[str] = set()
    if not text:
        return mentioned_files, mentioned_idents
    words = set(_WORD_RE.findall(text))
    for relpath, parsed in files.items():
        basename = relpath.rsplit("/", 1)[-1]
        if relpath in text or basename in text:
            mentioned_files.add(relpath)
        for d in parsed.defs:
            if d.name in words:
                mentioned_idents.add(d.name)
    return mentioned_files, mentioned_idents


def _rank(
    files: dict[str, FileParse],
    prompt: str,
    context_text: str,
) -> list[str]:
    """Relpaths ordered by personalised PageRank, best first."""
    relpaths = sorted(files)
    n = len(relpaths)
    if n == 0:
        return []
    index = {rel: i for i, rel in enumerate(relpaths)}

    # Name -> defining files (name-based resolution, as in Aider).
    defines: dict[str, list[str]] = {}
    for rel, parsed in files.items():
        for d in parsed.defs:
            defines.setdefault(d.name, []).append(rel)

    edges: dict[int, dict[int, float]] = {}
    for rel, parsed in files.items():
        out: dict[int, float] = {}
        for name, count in parsed.refs.items():
            for target in defines.get(name, []):
                if target != rel:
                    out[index[target]] = out.get(index[target], 0.0) + count
        if out:
            edges[index[rel]] = out

    prompt_files, prompt_idents = _mentions(prompt, files)
    ctx_files, ctx_idents = _mentions(context_text, files)

    # Mention weights are deliberately large next to the 1.0 base:
    # on a real repo a mentioned file must be able to outrank a
    # graph-central file the task never named.
    personal = [1.0] * n
    for rel in prompt_files:
        personal[index[rel]] += 50.0
    for rel in ctx_files:
        personal[index[rel]] += 25.0
    for name in prompt_idents:
        for rel in defines.get(name, []):
            personal[index[rel]] += 20.0
    for name in ctx_idents:
        for rel in defines.get(name, []):
            personal[index[rel]] += 8.0
    total = sum(personal)
    personal = [p / total for p in personal]

    out_weight = {i: sum(out.values()) for i, out in edges.items()}
    rank = list(personal)
    d = _PAGERANK_DAMPING
    for _ in range(100):
        new = [(1.0 - d) * p for p in personal]
        dangling = sum(rank[i] for i in range(n) if i not in edges)
        for i in range(n):
            new[i] += d * dangling * personal[i]
        for i, out in edges.items():
            for j, w in out.items():
                new[j] += d * rank[i] * (w / out_weight[i])
        delta = sum(abs(a - b) for a, b in zip(new, rank))
        rank = new
        if delta < 1e-6:
            break
    return sorted(relpaths, key=lambda rel: (-rank[index[rel]], rel))


# ---------------------------------------------------------------------------
# Rendering: ranked files, signatures only, hard token budget
# ---------------------------------------------------------------------------

def _render(files: dict[str, FileParse], ranked: list[str], token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    # Inbound reference counts per symbol name (from other files) —
    # the most-referenced definitions render first within a file.
    inbound: Counter = Counter()
    defines: dict[str, set[str]] = {}
    for rel, parsed in files.items():
        for d in parsed.defs:
            defines.setdefault(d.name, set()).add(rel)
    for rel, parsed in files.items():
        for name, count in parsed.refs.items():
            if any(t != rel for t in defines.get(name, set())):
                inbound[name] += count

    lines: list[str] = []

    def fits(extra: list[str]) -> bool:
        return estimate_tokens("\n".join(lines + extra)) <= token_budget

    # Per-file cap: a map is an index, not a listing — one huge file
    # must not eat the whole budget and starve every other file. A
    # file renders its most-referenced definitions up to the cap.
    per_file_cap = max(100, token_budget // 4)
    for rel in ranked:
        parsed = files[rel]
        if not parsed.defs:
            continue
        ordered = sorted(
            enumerate(parsed.defs),
            key=lambda pair: (-inbound.get(pair[1].name, 0), pair[0]),
        )
        block = [f"{rel}:"]
        for _i, d in ordered:
            line = ("  " if d.indent == 0 else "    ") + d.signature
            if len(block) > 1 and estimate_tokens("\n".join(block + [line])) > per_file_cap:
                break
            block.append(line)
        if fits(block):
            lines.extend(block)
            continue
        # Partial fill: header + as many top definitions as fit.
        partial = [block[0]]
        added = 0
        for line in block[1:]:
            if fits(partial + [line]):
                partial.append(line)
                added += 1
            else:
                break
        if added:
            lines.extend(partial)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_repo_map(
    workspace_roots: list[str],
    prompt: str = "",
    *,
    token_budget: int = 1000,
    context_text: str = "",
) -> str:
    """Render the repo map for *workspace_roots*, ranked toward the task.

    *prompt* is the current task's prompt; *context_text* is extra
    conversation text (files in play) given a weaker bias. Returns ""
    when there is nothing mappable or the budget disables the map.
    Best-effort by contract: any failure yields "" rather than raising
    into a chat path.
    """
    if token_budget <= 0 or not workspace_roots:
        return ""
    try:
        files = _load_files([str(r) for r in workspace_roots])
        if not files:
            return ""
        ranked = _rank(files, prompt, context_text)
        return _render(files, ranked, token_budget)
    except Exception:  # the map is ambient context; it must never break a task
        logger.warning("Repo map build failed", exc_info=True)
        return ""
