import base64
import binascii
import json
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.managed import RemainingSteps

# Tool-call loop guard (Epic 1, E1-03): this many identical consecutive
# tool calls (same tool, same arguments) trigger a corrective steer in
# execute_tool instead of another execution. Sized from Oh My Pi's
# proven default of 5. Lives here because the node that enforces it
# already imports this module (graph.py would be a circular import).
TOOL_LOOP_GUARD_THRESHOLD = 5


def to_aether_message(message: Any) -> Any:
    """Convert one LangChain ``BaseMessage`` to Aether's dict shape.

    Aether's message channel is dict-based (``{"role", "content", ...}``),
    but the prebuilt supervisor agent (Epic 1, ``create_supervisor``)
    writes real LangChain messages into the shared channel. Consumers
    that need plain dicts (specialist nodes building provider input, the
    executor translating updates into events) normalise through here.
    Dicts pass through unchanged.
    """
    if isinstance(message, dict) or not isinstance(message, BaseMessage):
        return message
    role = {
        "human": "user",
        "ai": "assistant",
        "system": "system",
        "tool": "tool",
        "function": "tool",
    }.get(message.type, message.type)
    converted: dict[str, Any] = {
        "role": role,
        "content": message.content if isinstance(message.content, str) else str(message.content),
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        converted["tool_calls"] = [
            {
                "id": tc.get("id"),
                "type": "function",
                "function": {
                    "name": tc.get("name"),
                    "arguments": json.dumps(tc.get("args", {})),
                },
            }
            for tc in tool_calls
        ]
    if message.type == "tool":
        converted["tool_call_id"] = getattr(message, "tool_call_id", None)
        if getattr(message, "name", None):
            converted["name"] = message.name
    # E3-F10: the supervisor's drop metadata lives in additional_kwargs
    # (aether_dropped_tool_calls). Normalizing it away here was one of
    # the two seams that lost dropped names from the latest state —
    # a batch crossing this boundary kept the call record but not the
    # record of what was dropped alongside it.
    dropped = (getattr(message, "additional_kwargs", None) or {}).get(
        "aether_dropped_tool_calls"
    )
    if dropped:
        converted["aether_dropped_tool_calls"] = list(dropped)
    return converted


def message_dicts(messages: Any) -> list[dict]:
    """Normalise a message list to plain Aether dicts (see above)."""
    return [to_aether_message(m) for m in (messages or [])]


def _valid_data_url(url: str) -> bool:
    """True when *url* is a usable data: URL.

    A base64 payload that does not decode is corrupt bytes, not an
    image; non-base64 data URLs (e.g. URL-encoded SVG) are accepted
    as-is — there is no byte payload to validate.
    """
    header, sep, payload = url.partition(",")
    if not sep:
        return False
    if ";base64" in header:
        try:
            base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return False
    return True


def _attachment_image_url(
    att: Any, workspace_roots: list[str] | None = None
) -> str | None:
    """Resolve one attachment to an image URL, or None when its bytes
    are unavailable/corrupt. Mirrors fast chat's resolution order
    (inline ``data``/``url`` first, then a file ``path`` read into a
    data: URL); remote http(s) URLs pass through unverified, exactly
    as fast chat passes them.

    S1-M8-ADJ (xref S1-M8): a file ``path`` goes through fast chat's
    S1-M8 loader — containment against the sanctioned roots (the
    engine attachments dir plus the granted workspace roots), the
    image-mime allow-list and the 10 MB cap — instead of a bare
    ``read_bytes()`` of any local file. A refusal degrades to None
    here (the caller renders the "could not be loaded" note), which
    is this path's contract, unlike fast chat's raise-and-fail-turn.
    The fast-chat import is lazy: fast_chat imports the executor,
    which imports this module, so a module-level import would cycle.
    """
    url: Any = None
    path: Any = None
    if isinstance(att, dict):
        url = att.get("data") or att.get("url")
        path = att.get("path")
    elif isinstance(att, str):
        if att.startswith(("data:", "http://", "https://")):
            url = att
        else:
            path = att
    if url is None and path:
        if isinstance(path, str) and path.startswith("data:"):
            url = path
        else:
            from aether_engine.langgraph.fast_chat import (
                AttachmentRejectedError,
                _load_path_attachment,
                _sanctioned_attachment_roots,
            )

            try:
                url = _load_path_attachment(
                    path,
                    att.get("mime_type") if isinstance(att, dict) else None,
                    _sanctioned_attachment_roots(workspace_roots),
                )
            except AttachmentRejectedError:
                url = None
    if isinstance(url, str) and url:
        if url.startswith("data:") and not _valid_data_url(url):
            return None
        return url
    return None


def _attachment_label(att: Any) -> str:
    if isinstance(att, dict):
        name = att.get("name") or att.get("path")
        if name:
            return Path(str(name)).name or str(name)
    elif isinstance(att, str) and att and not att.startswith(("data:", "http")):
        return Path(att).name or att
    return "attachment"


def multimodal_user_content(
    prompt: str,
    attachments: list[Any] | None,
    workspace_roots: list[str] | None = None,
) -> Any:
    """The user-message content for *prompt* + *attachments*.

    The single builder for the multimodal shape both chat paths use
    (Grill Q20: the same attachment flow for fast chat and graph
    tasks): with no attachments the content is the plain prompt
    string, byte-identical to a text-only message; with attachments
    it is the parts list the provider layer already converts —
    ``{"type": "text"}`` for the prompt, ``{"type": "image_url",
    "image_url": {"url": ...}}`` per image (fast_chat.py's shape).

    An attachment whose bytes are missing or corrupt degrades to an
    honest text note in the message — never a crash, never a silent
    drop, never a bogus image part.
    """
    if not attachments:
        return prompt
    parts: list[dict[str, Any]] = []
    if prompt:
        parts.append({"type": "text", "text": prompt})
    for att in attachments:
        url = _attachment_image_url(att, workspace_roots=workspace_roots)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        else:
            parts.append({
                "type": "text",
                "text": (
                    f'[Attachment "{_attachment_label(att)}" could not be '
                    "loaded, so it was not sent to the model.]"
                ),
            })
    return parts or prompt


def collapse_subgraph_echo(left: list, right: list) -> list:
    """Reducer for channels shared with compiled subgraphs.

    A subgraph (specialist agent, supervisor agent) receives this
    channel's full value as its input and returns its full final value
    as the node update — including the handoff ``Command`` in
    langgraph-supervisor, which explicitly writes ``{**state, ...}``
    back to the parent. A plain ``operator.add`` would therefore append
    a second copy of the entire history on every delegation. When the
    incoming list starts with the whole current list (the echo shape),
    it IS the new value, not an append.

    LangChain messages riding in a multi-message batch are converted to
    Aether dicts: batches that large only occur at graph boundaries
    (subgraph outputs, handoff commands, run input), never inside the
    supervisor agent, whose own single-message writes must stay real
    ``BaseMessage`` objects for its internal tool routing.
    """
    left = list(left or [])
    right = list(right or [])
    if len(right) > 1:
        right = [to_aether_message(m) for m in right]
    if left and len(right) >= len(left) and right[: len(left)] == left:
        return right
    return left + right


# ── Shared blackboard (Epic 1, E1-15) ────────────────────────────────
# The agents' common working state, carried in the graph state itself
# (owner's choice: LangGraph state, not a parallel store) and persisted
# by the checkpointer across pause/resume/crash. Sections:
#   plan           — str, overwritten by whoever plans last
#   findings       — list[str], append-merged (research results, facts)
#   decisions      — list[str], append-merged (delegation/commitments)
#   open_questions — list[str], overwritten (a question leaves the list
#                    by being answered, so append semantics cannot work)
#   artifacts      — list[str], append-merged (files the team produced)
# EVERY section is bounded (E1-F14): list sections keep at most
# BLACKBOARD_LIST_CAP entries of at most BLACKBOARD_ENTRY_MAX_CHARS
# each, and the plan is capped at BLACKBOARD_PLAN_MAX_CHARS — an agent
# writing a 100k plan or 60 questions cannot bloat the checkpoint.
# The board also carries a bounded change journal ("_journal") so the
# briefing can show each agent what changed since its own last turn.
BLACKBOARD_LIST_CAP = 50
BLACKBOARD_PLAN_MAX_CHARS = 4000
BLACKBOARD_ENTRY_MAX_CHARS = 1000
BLACKBOARD_JOURNAL_CAP = 50
BLACKBOARD_BRIEFING_MAX_CHARS = 1200

_BLACKBOARD_APPEND_SECTIONS = ("findings", "decisions", "artifacts")
_BLACKBOARD_SECTION_LABELS = {
    "plan": "Plan",
    "findings": "Findings",
    "decisions": "Decisions",
    "open_questions": "Open questions",
    "artifacts": "Artifacts",
}


def merge_mapping(left: dict | None, right: dict | None) -> dict:
    """Key-wise merge for dict channels written per-agent (E1-10)."""
    return {**(left or {}), **(right or {})}


def _cap_board_entry(value: Any) -> Any:
    """Bound one blackboard list entry's size (E1-F14)."""
    if isinstance(value, str) and len(value) > BLACKBOARD_ENTRY_MAX_CHARS:
        return value[:BLACKBOARD_ENTRY_MAX_CHARS]
    return value


def merge_blackboard(left: dict | None, right: dict | None) -> dict:
    """Reducer for the blackboard channel (see the section rules above).

    Subgraph outputs echo the full current value back (the same shape
    collapse_subgraph_echo handles for lists): when an incoming list
    starts with the whole current list, it IS the new value, not an
    append — otherwise every delegation would duplicate every entry.
    """
    merged: dict[str, Any] = dict(left or {})
    # The change journal merges by sequence number (union), so an echo
    # of the full board never duplicates or rewinds it.
    journal_by_seq: dict[int, dict] = {}
    for source in (left, right):
        for entry in (source or {}).get("_journal") or []:
            if isinstance(entry, dict) and isinstance(entry.get("seq"), int):
                journal_by_seq[entry["seq"]] = entry
    journal: list[dict] = [journal_by_seq[s] for s in sorted(journal_by_seq)]
    next_seq = (journal[-1]["seq"] if journal else 0) + 1

    def _note(section: str, text: str) -> None:
        nonlocal next_seq
        journal.append({"seq": next_seq, "section": section, "text": text})
        next_seq += 1

    for key, value in (right or {}).items():
        if key == "_journal":
            continue
        if key in _BLACKBOARD_APPEND_SECTIONS and isinstance(value, list):
            current = list(merged.get(key) or [])
            incoming = list(value)
            if current and len(incoming) >= len(current) and incoming[: len(current)] == current:
                new_entries = incoming[len(current):]
                merged[key] = [_cap_board_entry(e) for e in incoming[-BLACKBOARD_LIST_CAP:]]
            else:
                new_entries = incoming
                merged[key] = [_cap_board_entry(e) for e in (current + incoming)[-BLACKBOARD_LIST_CAP:]]
            for entry in new_entries:
                _note(key, str(entry)[:BLACKBOARD_ENTRY_MAX_CHARS])
        elif key == "plan":
            capped = value[:BLACKBOARD_PLAN_MAX_CHARS] if isinstance(value, str) else value
            if capped != merged.get("plan"):
                _note("plan", "plan updated")
            merged[key] = capped
        elif key == "open_questions" and isinstance(value, list):
            capped = [_cap_board_entry(e) for e in list(value)[-BLACKBOARD_LIST_CAP:]]
            if capped != list(merged.get("open_questions") or []):
                _note("open_questions", f"open questions updated ({len(capped)} open)")
            merged[key] = capped
        else:
            merged[key] = value
    if journal:
        merged["_journal"] = journal[-BLACKBOARD_JOURNAL_CAP:]
    elif "_journal" in merged:
        del merged["_journal"]
    return merged


# Per-(task, agent) briefing cursors for the blackboard delta (E1-F14):
# the journal sequence each agent had already seen at its last
# briefing. Process-local on purpose — the delta is a prompt
# optimisation, not state of record: the journal itself is
# checkpointed with the board, so after an engine restart a cursor
# simply re-baselines (the full summary always renders regardless).
_BRIEFING_CURSORS: "OrderedDict[tuple[str, str], int]" = OrderedDict()
_BRIEFING_CURSOR_CAP = 256


def _blackboard_delta(state: dict, board: dict, agent: str | None) -> str:
    """What changed on the board since the reading agent's last turn.

    Keyed by (task_id, agent): the first briefing in a task sets the
    agent's baseline and shows no delta; later briefings list the
    journal entries past the agent's cursor, then advance it.
    """
    reader = agent or (state or {}).get("active_specialist")
    task_id = (state or {}).get("task_id")
    journal = board.get("_journal") or []
    if not reader or not task_id or not journal:
        return ""
    key = (str(task_id), str(reader))
    latest = max(e["seq"] for e in journal)
    cursor = _BRIEFING_CURSORS.get(key)
    _BRIEFING_CURSORS[key] = latest
    _BRIEFING_CURSORS.move_to_end(key)
    while len(_BRIEFING_CURSORS) > _BRIEFING_CURSOR_CAP:
        _BRIEFING_CURSORS.popitem(last=False)
    if cursor is None:
        return ""
    fresh = [e for e in journal if e["seq"] > cursor]
    if not fresh:
        return ""
    rendered = "; ".join(
        f"{_BLACKBOARD_SECTION_LABELS.get(e['section'], e['section'])}: {e['text']}"
        for e in fresh
    )
    return f"New since your last turn: {rendered}"


def blackboard_briefing(state: dict, *, agent: str | None = None,
                        max_chars: int = BLACKBOARD_BRIEFING_MAX_CHARS) -> str:
    """Render the blackboard as a bounded prompt section.

    The optimisation the owner asked for: agents never receive the raw
    blackboard, only this summary — plan excerpt, the most recent
    findings/decisions/artifacts, open questions, and (for an
    identified reader) what changed since that agent's last turn —
    hard-capped at max_chars regardless of how much the board holds.
    """
    board = (state or {}).get("blackboard") or {}
    parts: list[str] = []
    plan = (board.get("plan") or "").strip()
    if plan:
        parts.append(f"Plan: {plan[:300]}")
    for key, label in (("findings", "Findings"), ("decisions", "Decisions"),
                       ("artifacts", "Artifacts")):
        entries = [str(e).strip() for e in (board.get(key) or []) if str(e).strip()]
        if entries:
            recent = entries[-5:]
            parts.append(f"{label} (latest {len(recent)} of {len(entries)}): " + " | ".join(recent))
    questions = [str(q).strip() for q in (board.get("open_questions") or []) if str(q).strip()]
    if questions:
        parts.append("Open questions: " + " | ".join(questions[-5:]))
    delta = _blackboard_delta(state, board, agent)
    if delta:
        parts.append(delta)
    text = "\n".join(parts)
    return text[:max_chars]


class AetherState(TypedDict):
    original_prompt: str

    # Messages accumulate, with subgraph-echo collapse (see the reducer).
    messages: Annotated[list[Any], collapse_subgraph_echo]

    # These fields OVERWRITE the existing value rather than accumulate
    plan: str
    current_phase: str
    artifact_paths: dict[str, str]
    tool_results_summary: dict[str, str]
    pending_tool_call: dict[str, Any] | None

    # Parallel tool calls (S1-M5): calls a specialist emitted in one
    # model response beyond the first, as pending-shaped dicts
    # ({"name", "args", "call_id"}). Plain overwrite channel: each
    # specialist return writes the full remaining queue, drained one
    # call per tool-loop round by the specialist node itself (see
    # nodes/tool_calls.drain_queued_tool_call). Checkpointed in state
    # so the queue survives the approval pause.
    queued_tool_calls: list[dict[str, Any]]
    active_specialist: str | None

    # Tool-call loop guard (Epic 1, E1-03): signature of the most recent
    # tool call and how many times it has repeated consecutively.
    tool_call_signature: str | None
    tool_call_repeats: int

    # Prewalk (Epic 1, E1-13): set by execute_tool once a non-READ_ONLY
    # tool has actually executed; prewalk-enabled specialists plan on
    # the run provider until this flips, then switch to their per-agent
    # (execution) model.
    write_tool_used: bool

    # Structured log entries for delegation decisions (echo-collapsed
    # like messages: the supervisor agent returns the full log).
    delegation_log: Annotated[list[dict[str, Any]], collapse_subgraph_echo]

    # Shared agent blackboard (Epic 1, E1-15): every agent reads and
    # writes it through state updates; see merge_blackboard for the
    # per-section merge rules and blackboard_briefing for how it is
    # injected into prompts (bounded summary, never a raw dump).
    blackboard: Annotated[dict[str, Any], merge_blackboard]

    # Structured outputs (Epic 1, E1-10): agent name -> parsed value
    # validated against the agent's configured output schema (or
    # {"error": ...} after a failed correction retry). Key-merged so
    # specialists never erase each other's entries.
    structured_outputs: Annotated[dict[str, Any], merge_mapping]

    # Plan mode (Epic 1, E1-09): when True, specialists only receive
    # READ_ONLY tools and execute_tool refuses anything riskier. Lives
    # in state (not just config) so it survives the approval pause via
    # the checkpointer with no resume-time threading.
    plan_mode: bool

    # Conversation compaction (Epic 1, E1-06): a summary of the older
    # turns of this thread's history, and how many channel messages it
    # covers. The messages channel itself is NEVER rewritten by
    # compaction — models see summary + tail through
    # compaction.model_facing_messages, and the full history stays in
    # the checkpoint. The marker makes compaction incremental: the
    # next round folds only messages beyond it.
    context_summary: str | None
    context_summary_upto: int

    # Session and task identifiers
    task_id: str | None
    session_id: str | None

    # Provenance (N08): the client-generated id of the user message
    # that originated this task turn. The UI mints that id and the
    # session store keeps it as the message row's id, so it is the one
    # turn message with a stable identity live and after reload.
    # Media the task generates is registered against it so the
    # Library's "jump to conversation" lands on the exact message.
    # Lives in state (not just config), like plan_mode, so it
    # survives the approval pause via the checkpointer — most
    # generated files are written by the approved tool call itself,
    # which executes on the resume path.
    # PF-3: applying a steer replaces this with the steer message's
    # own id (see steering.steer_state_update), so media generated
    # during the steered continuation attributes to the message that
    # prompted it rather than the turn's original message.
    message_id: str | None

    context_briefing: str | None
    active_skills: list[str]

    # Effective run configuration snapshot (E1-F04): the execution
    # settings this run was resolved with — provider/model names,
    # per-agent models/tools, forced agent, workspace roots, the
    # layered (effective) user config and the routing constraints —
    # as plain JSON-safe data, seeded once by run_langgraph_task.
    # Like plan_mode it lives in state so the checkpointer carries it
    # across the approval pause: a resume (possibly on a fresh
    # connection, possibly after an engine restart) rebuilds the
    # run's OWN configuration from it instead of the then-current
    # globals. Live objects (the provider instance, the tool
    # registry) are rebuilt from these names/values at resume time;
    # they are never stored here.
    run_config: dict[str, Any] | None

    # Managed by the LangGraph runtime; required in the schema by the
    # prebuilt supervisor agent (langgraph-supervisor / create_react_agent).
    remaining_steps: NotRequired[RemainingSteps]
