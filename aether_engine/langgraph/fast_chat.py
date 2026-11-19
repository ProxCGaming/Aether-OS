"""Fast-chat runner (Epic 1, E1-04).

Streams a direct reply from the resolved chat provider for plain
conversation: no supervisor graph, no tool loop, no checkpoint writes.

It yields the same Event contract as ``run_langgraph_task``
(TASK_CREATED -> TASK_PROGRESS deltas -> TASK_COMPLETED / TASK_FAILED)
so the websocket layer's ``execute_task_generator`` handles routing
stamps, audit transitions, health recording and assistant-message
persistence exactly as it does for graph tasks — including the
CANCEL_TASK path, where cancelling the runner task throws
``asyncio.CancelledError`` into this generator at its current await,
exactly as it does into the graph runner.

History comes from the session store — the same store the supervisor's
``get_current_chat_history`` memory tool reads — so a conversation can
move between the fast path and the graph without losing its thread:
fast-chat turns are saved to the store by the shared websocket wrapper,
and a later graph task retrieves them from there.
"""
import asyncio
import base64
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from aether_common.contracts import Event, EventType, TaskState, validate_transition
from aether_engine.langgraph.executor import _is_retriable_error
from aether_engine.memory import session_store
from aether_engine.providers.base import BaseProvider
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.tools.paths import (
    PathContainmentError,
    effective_roots,
    resolve_contained,
)

logger = logging.getLogger("aether_engine.langgraph.fast_chat")

_HISTORY_LIMIT = 20
_HISTORY_MAX_CHARS = 24_000

_SYSTEM_PROMPT = (
    "You are Aether, a helpful AI assistant. Answer the user directly and "
    "conversationally, using Markdown when it improves readability. If the "
    "user asks for something that requires tools, files or live research, "
    "say so briefly instead of inventing results."
)

# Epic 8, E8-3.1 (grill Q18): fast chat NEVER receives a repo map —
# the map is a graph-task-only ambient layer, built by
# rag/repo_map.py and injected by the supervisor. Do not import it
# here; tests/test_repo_map.py pins this exclusion.

# ---------------------------------------------------------------------------
# S1-M8: attachment path containment.
#
# A client-supplied attachment ``path`` used to be read with a bare
# ``Path(...).read_bytes()`` and shipped to the cloud provider as image
# content — any readable local file (``/etc/passwd``, ``~/.ssh/...``)
# could be exfiltrated through a chat turn. A local path may now be
# read ONLY when its fully resolved form (symlinks followed, the same
# resolve-then-check rule ``tools/paths.py`` applies to tool paths)
# sits inside a sanctioned root:
#   * the attachments directory the engine itself manages
#     (``~/.aether/attachments`` — where the websocket handler
#     materialises uploaded/snipped images per session), and
#   * the granted workspace roots for the run (``tools/paths.py``
#     ``effective_roots`` semantics: the configured roots, falling back
#     to ``~/AetherWorkspace``).
# The file must also be an image on the allow-list below — judged by
# extension, and by the declared mime when the client supplies one —
# and no larger than MAX_ATTACHMENT_BYTES. Anything else rejects the
# whole turn with an honest TASK_FAILED: never read-and-send, never a
# silent drop. Inline ``data:`` URLs and remote http(s) URLs keep
# their long-standing pass-through behaviour (no local read happens).
# ---------------------------------------------------------------------------

_ATTACHMENT_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_ATTACHMENT_MIMES = frozenset(_ATTACHMENT_MIME_BY_EXT.values())

# No attachment-specific cap existed before S1-M8; 10 MB matches the
# engine's other byte caps (the audit log's rotation bound) and sits
# well above any real screenshot while keeping a single turn's payload
# sane.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class AttachmentRejectedError(ValueError):
    """An attachment path failed containment / type / size validation."""


def _attachments_root() -> Path:
    """The engine-managed attachments directory (sanctioned root).

    The websocket handler materialises data-URL uploads under
    ``<root>/<session_id>`` (``routes/websocket.py``
    ``_register_message_attachments``); the whole tree is the
    attachments directory sessions read back from.
    """
    return Path.home() / ".aether" / "attachments"


def _sanctioned_attachment_roots(workspace_roots: list[str] | None) -> list[str]:
    roots = [str(_attachments_root())]
    roots.extend(str(r) for r in effective_roots(workspace_roots))
    return roots


def _load_path_attachment(
    raw_path: Any,
    declared_mime: str | None,
    roots: list[str],
) -> str:
    """Read a local-file attachment into a ``data:`` URL, or raise
    :class:`AttachmentRejectedError` with an honest, client-safe
    reason. The file's bytes are read only after containment, type
    and size have all passed."""
    label = Path(str(raw_path)).name or str(raw_path)
    try:
        resolved = resolve_contained(str(raw_path), roots)
    except PathContainmentError:
        raise AttachmentRejectedError(
            f'Attachment "{label}" was refused: its path is outside the '
            "folders Aether is allowed to read attachments from."
        ) from None
    if not resolved.is_file():
        raise AttachmentRejectedError(
            f'Attachment "{label}" was refused: the file does not exist.')
    mime = _ATTACHMENT_MIME_BY_EXT.get(resolved.suffix.lower())
    if mime is None:
        raise AttachmentRejectedError(
            f'Attachment "{label}" was refused: only image attachments '
            f"({', '.join(sorted(_ATTACHMENT_MIMES))}) can be sent.")
    if declared_mime and declared_mime not in _ATTACHMENT_MIMES:
        raise AttachmentRejectedError(
            f'Attachment "{label}" was refused: its declared type '
            f'"{declared_mime}" is not a supported image type.')
    try:
        size = resolved.stat().st_size
    except OSError:
        raise AttachmentRejectedError(
            f'Attachment "{label}" was refused: the file could not be read.'
        ) from None
    if size > MAX_ATTACHMENT_BYTES:
        raise AttachmentRejectedError(
            f'Attachment "{label}" was refused: it is larger than the '
            f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB attachment limit.")
    try:
        data = resolved.read_bytes()
    except OSError:
        raise AttachmentRejectedError(
            f'Attachment "{label}" was refused: the file could not be read.'
        ) from None
    if len(data) > MAX_ATTACHMENT_BYTES:  # grew between stat and read
        raise AttachmentRejectedError(
            f'Attachment "{label}" was refused: it is larger than the '
            f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB attachment limit.")
    return f"data:{declared_mime or mime};base64,{base64.b64encode(data).decode('ascii')}"


def _resolve_attachment_url(att: Any, roots: list[str]) -> str | None:
    """Resolve one attachment to an image URL.

    Mirrors the historical resolution order (inline ``data``/``url``
    first, then a file ``path``): ``data:`` and http(s) URLs pass
    through untouched, a file path goes through
    :func:`_load_path_attachment` (raising on refusal), and an
    attachment carrying neither URL nor path resolves to None,
    exactly as before.
    """
    if isinstance(att, dict):
        url = att.get("data") or att.get("url")
        if url:
            return url
        path = att.get("path")
        if not path:
            return None
        if isinstance(path, str) and path.startswith("data:"):
            return path
        return _load_path_attachment(path, att.get("mime_type"), roots)
    if isinstance(att, str):
        if att.startswith(("data:", "http://", "https://")):
            return att
        return _load_path_attachment(att, None, roots)
    return None


def load_session_history(
    session_id: str | None,
    current_prompt: str | None = None,
    *,
    limit: int = _HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    """Prior user/assistant turns for *session_id* from the session store.

    The START_TASK handler saves the current user message to the store
    before the fast/graph split, so a trailing user message identical to
    *current_prompt* is that just-saved turn and is excluded — the runner
    appends the prompt itself.
    """
    if not session_id:
        return []
    try:
        # The shaping lives in the session store (shared with the graph
        # executor's fresh-thread seeding); this wrapper only keeps the
        # best-effort contract: a broken store yields no history.
        return session_store.get_chat_history(
            session_id, current_prompt, limit=limit, max_chars=_HISTORY_MAX_CHARS)
    except Exception:  # History is best-effort; a broken store must not break the chat turn.
        logger.debug("Fast chat: session history unavailable", exc_info=True)
        return []


async def learn_fast_chat_exchange(
    task_id: str,
    prompt: str,
    response_text: str,
    provider: BaseProvider | None,
    user_config=None,
) -> None:
    """Epic 4, E4-05: learn a completed fast-chat exchange.

    Graph tasks learn on the executor's completion path; fast chat
    has no executor, so the websocket wrapper calls this in a
    detached task after TASK_COMPLETED. Through the memory facade,
    with the chat's own provider doing fact extraction when one is
    available (the extractor's greeting/substance guards apply as
    usual). Best-effort by contract: callers fire-and-forget, and
    every failure is logged here, never raised into the chat path.
    """
    from aether_engine.memory.facade import backend_or_none

    # Wave 6 (D-5, SPEC-6B C4): the write gate — with
    # memory_learn_enabled off, a fast-chat exchange stores
    # nothing: no episode, no facts, no lessons. The stored data
    # is untouched (the gate is on the write, not the store);
    # turning learning back on resumes from the next exchange.
    try:
        config = user_config
        if config is None:
            from aether_engine.config import load_config

            config = load_config()
        if not getattr(config, "memory_learn_enabled", True):
            return
    except Exception:  # a gate-read fault must not break learning
        logger.debug("Fast-chat memory learn gate unavailable", exc_info=True)

    # S8 (14 Nov 2026): never learn from a forget/undo exchange. The
    # reply to a forget command quotes the deleted item (the engine's
    # note enumerates it), so extraction over this exchange could
    # re-store a fresh fact about — or even containing — the very
    # thing the user just ordered forgotten. A forget command is an
    # instruction to memory, not content for memory: no episode, no
    # facts, no lessons are recorded from it.
    try:
        from aether_engine.memory.forget import detect_forget_intent

        if detect_forget_intent(prompt) is not None:
            return
    except Exception:  # a detector fault must not break learning
        logger.debug("Fast-chat forget-intent check failed", exc_info=True)

    try:
        backend = backend_or_none(user_config, context="fast-chat memory")
        if backend is None:
            return
        await backend.record_episode(
            task_id=task_id,
            user_prompt=prompt,
            task_summary=response_text[:200],
            tags=["chat", "fast"],
            outcome=response_text,
        )
        if provider is not None:
            await backend.learn(
                provider=provider,
                user_prompt=prompt,
                assistant_response=response_text,
                task_id=task_id,
            )
            # Epic 4, E4-10: fast-chat completions feed the learn
            # loop too — lessons from a chat exchange are lessons.
            await backend.learn_lessons(
                provider=provider,
                user_prompt=prompt,
                assistant_response=response_text,
                task_id=task_id,
            )
    except Exception as e:  # noqa: BLE001 — background learn must never surface in the chat path
        logger.warning(f"Fast-chat memory learn failed for {task_id}: {e}")


async def _record_message_point(
    session_id: str | None, task_id: str, step_index: int, label: str
) -> None:
    """Epic 8, E8-1.1a: append a message-level rewind point for a
    fast-chat task. Fast chat has no graph checkpoint, so the point's
    checkpoint_id is "" — honest, never invented. Best-effort: a
    manifest failure must never break the chat turn."""
    if not session_id:
        return
    try:
        await asyncio.to_thread(
            rewind_manifest.record_point,
            session_id, task_id, step_index, "", label, [],
        )
    except Exception:  # best-effort by doctrine (see docstring)
        logger.debug("Fast-chat rewind manifest recording failed", exc_info=True)


# ---------------------------------------------------------------------------
# S7: fast-chat memory briefing (master-build plan §4 S7, R9).
#
# Fast chat is the default conversational surface and the only
# voice surface (voice/pipeline.py's talker adapts this runner),
# and it was the one surface that never saw long-term memory: the
# prompt carried the persona, the session history and C3's
# one-line calendar itinerary — no KG facts, no episodes, no
# profile, no lessons, while graph tasks got all of them. This
# briefing closes that gap under the house briefing contract (the
# calendar_briefing() pattern): capped, empty-when-absent, and it
# NEVER breaks or blocks the turn.
#
# Composition — one system message, three blocks sharing ONE
# sub-budget (UserConfig.fast_chat_memory_token_budget, default
# 400 tokens at the house 4 chars/token estimate), in priority
# order:
#   1. the profile digest — a compact "who this user is" rendered
#      from the S6 profile view's structured data (facade
#      user_profile()): top stated facts about the user, the
#      people's names, top preferences; hard-capped at
#      _PROFILE_DIGEST_MAX_CHARS, inferred items labelled
#      "(inferred)" exactly as the profile briefing labels them;
#   2. the S5 assembler's scored recall for THIS message (KG
#      facts + past episodes through the facade, hybrid
#      candidates per S6) — the sub-budget remainder is passed
#      INTO assemble_memory_context as its budget_tokens, so
#      there is still exactly one scoring path and one budget
#      discipline, never a second ranker;
#   3. lessons (learned_briefing) filling whatever budget the
#      first two blocks leave, when the remainder is worth a
#      lessons block at all.
# The header states plainly that this is long-term memory and
# that the live conversation wins a conflict — the same honesty
# the supervisor's memory framing carries.
#
# Failure contract: memory is additive, never a new failure mode
# for chat. The toggle off, an unavailable backend, an empty
# memory, or ANY exception in assembly returns "" and the caller
# appends nothing — the messages are then exactly the pre-S7
# composition. Assembly adds no LLM calls and no network calls
# beyond the query embedding the S6 hybrid leg already makes
# (which degrades to substring-only when embeddings are down);
# its cost is logged at debug so the voice turn budget can be
# audited against it.
# ---------------------------------------------------------------------------

_MEMORY_SECTION_HEADER = (
    "What Aether remembers (long-term memory — it can be incomplete "
    "or out of date; if it conflicts with what the user says in this "
    "conversation, believe the user):"
)

# The digest's share of the section: ~100 tokens at the house
# estimate. Identity context is small by nature — a few stated
# facts, the people's names, the top preferences.
_PROFILE_DIGEST_MAX_CHARS = 400

# Below this remainder a lessons block would be its header plus a
# truncation marker, which teaches the model nothing.
_LESSONS_MIN_TOKENS = 40


def _profile_digest(profile: dict[str, Any] | None) -> str:
    """The compact profile digest from the S6 view's data.

    Consumes the dict facade.user_profile() returns (the profile
    is re-derived from the stored facts on every call — there is
    nothing here to keep in sync). Stated facts lead (the view's
    own trust order), inferred items keep their "(inferred)"
    label, and the result is hard-capped at
    _PROFILE_DIGEST_MAX_CHARS, cut at a line boundary — except a
    single over-long first line, which is truncated rather than
    dropped: identity context with one cut fact beats none.
    """
    if not profile:
        return ""

    def _shown(items: list[dict], limit: int) -> str:
        parts = []
        for item in items[:limit]:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            label = " (inferred)" if item.get("provenance") == "inferred" else ""
            parts.append(f"{text}{label}")
        return "; ".join(parts)

    lines: list[str] = []
    user_line = _shown(profile.get("user") or [], 3)
    if user_line:
        lines.append(f"About the user: {user_line}")
    people = profile.get("people") or {}
    if people:
        lines.append("People in the user's life: " + ", ".join(people))
    pref_line = _shown(profile.get("preferences") or [], 2)
    if pref_line:
        lines.append(f"Preferences: {pref_line}")

    digest = ""
    for line in lines:
        candidate = line if not digest else f"{digest}\n{line}"
        if len(candidate) <= _PROFILE_DIGEST_MAX_CHARS:
            digest = candidate
            continue
        if not digest:
            digest = line[: _PROFILE_DIGEST_MAX_CHARS - 1].rstrip() + "…"
        break
    return digest


async def fast_chat_memory_briefing(
    prompt: str,
    user_config=None,
    *,
    used_items_out: list | None = None,
) -> str:
    """The per-turn memory section for fast chat's prompt, or "".

    ``user_config`` is the run's resolved config when the caller
    has one (the websocket task path); voice and other callers
    pass nothing and the saved config is loaded, mirroring
    fast_chat_itinerary_line(). Never raises — see the S7 block
    comment above for the full contract.

    ONE builder (S16): the voice speculation preparer prepares
    and adopts this function's TEXT, and run_fast_chat calls the
    same function — passing ``used_items_out`` (a list) to also
    receive the SPEC-6B C6 disclosure entries for the items that
    scored into the section (profile digest items first, then
    the assembler's scored facts/episodes — deduped by id,
    capped at 5, summaries only). The list is left untouched
    whenever no memory section was built — memory off, no
    backend, or nothing scored.
    """
    section, used = await _fast_chat_memory_briefing_impl(
        prompt, user_config)
    if used_items_out is not None:
        used_items_out.extend(used)
    return section


async def _fast_chat_memory_briefing_impl(
    prompt: str,
    user_config=None,
) -> tuple[str, list[dict[str, Any]]]:
    """The briefing builder: (section text, C6 disclosure
    entries) — see fast_chat_memory_briefing for the contract."""
    started = time.perf_counter()
    try:
        config = user_config
        if config is None:
            from aether_engine.config import load_config

            config = load_config()
        # Wave 6 (D-5, SPEC-6B C4): the read gate is the pair's
        # memory_use_enabled, on this path exactly as on the
        # supervisor's — the retired fast_chat_memory_enabled
        # only seeds it at config load (config.py).
        if not getattr(config, "memory_use_enabled", True):
            return "", []
        budget = int(getattr(config, "fast_chat_memory_token_budget", 400) or 0)
        if budget <= 0:
            return "", []

        from aether_engine.memory.context_assembler import (
            MEMORY_USED_LIMIT,
            assemble_memory_context,
            estimate_tokens,
            memory_used_entries,
            profile_used_entries,
        )
        from aether_engine.memory.facade import backend_or_none

        backend = backend_or_none(config, context="fast-chat memory briefing")
        if backend is None:
            return "", []

        profile = backend.user_profile()
        digest = _profile_digest(profile)
        # Budget accounting in per-part ceilings — each part's
        # estimate includes its join newline, the assembler's own
        # convention — so when the parts' ceilings sum inside the
        # budget, the joined section's estimate is inside it too.
        used = estimate_tokens(_MEMORY_SECTION_HEADER + "\n")
        if digest:
            used += estimate_tokens(digest + "\n")
        assembled = await assemble_memory_context(
            backend, prompt, config=config,
            budget_tokens=max(0, budget - used))
        used += assembled["estimated_tokens"]

        blocks = [block for block in (digest, assembled["text"]) if block]
        remaining = budget - used
        if remaining >= _LESSONS_MIN_TOKENS:
            from aether_engine.memory.learned import learned_briefing

            lessons = learned_briefing(max_chars=remaining * 4)
            if lessons and estimate_tokens(lessons + "\n") <= remaining:
                blocks.append(lessons)
        if not blocks:
            return "", []
        section = "\n".join([_MEMORY_SECTION_HEADER, *blocks])
        # C6 disclosure: exactly the items the section above
        # carries — the digest's profile items (only when the
        # digest made the section) and the assembler's scored
        # items (its items exist only when its text does). A
        # fact the digest showed as a profile item is not
        # reported twice when recall also scored it: one entry
        # per id, profile form first. Lessons carry no per-item
        # identity and are not disclosed (context_assembler's
        # C6 section comment).
        used_entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        candidates = (
            (profile_used_entries(profile) if digest else [])
            + memory_used_entries(assembled["items"], limit=MEMORY_USED_LIMIT))
        for entry in candidates:
            if entry["id"] in seen_ids:
                continue
            seen_ids.add(entry["id"])
            used_entries.append(entry)
            if len(used_entries) >= MEMORY_USED_LIMIT:
                break
        logger.debug(
            "Fast-chat memory briefing: %d scored items, digest=%s, "
            "assembled in %.1f ms (est %d/%d tokens)",
            len(assembled["items"]), bool(digest),
            (time.perf_counter() - started) * 1000,
            estimate_tokens(section), budget)
        return section, used_entries
    except Exception:
        # Memory is additive; it must never break a chat turn.
        logger.debug("Fast-chat memory briefing unavailable", exc_info=True)
        return "", []


# ---------------------------------------------------------------------------
# S11: EQ briefing (master-build plan §4 S11, R5).
#
# Beside the S7 memory section rides a second, independent
# adaptation: aether_engine/eq.py reads the recent conversation's
# signals (frustration, corrections, re-asked questions, hurry,
# confusion) locally and deterministically — no LLM call, no
# memory access — and renders BEHAVIOUR guidance (pace,
# directness, simplify, offer a stopping point), never a label
# for the user and never a claim about their feelings. Same
# house contract as the briefings above: capped, empty when the
# read is neutral or low-confidence, and it NEVER breaks the
# turn. The toggle (UserConfig.eq_enabled) off, or any failure,
# leaves the message list exactly as it was — the pre-S11
# composition byte for byte.
#
# The read also leaves this runner through the optional
# on_affect_read callback: voice is the one consumer that holds
# per-session state outside the prompt (the pipeline records it
# via note_affect_read). Nothing is persisted — the read lives
# for the turn, and in a voice session for the session, only.
# ---------------------------------------------------------------------------

def _eq_read_for_turn(
    prompt: str,
    history: list[dict[str, Any]] | None,
    user_config=None,
):
    """The EQ affect read for this turn, or None.

    None when the toggle is off or anything fails — the caller
    then appends nothing. Never raises. Inputs are only what
    fast chat already holds: the session history's user turns
    plus the current prompt. Fast chat has no task-outcome
    stream, so outcomes are honestly None (the failure-streak
    signal simply never fires on this path).
    """
    try:
        config = user_config
        if config is None:
            from aether_engine.config import load_config

            config = load_config()
        if not getattr(config, "eq_enabled", True):
            return None
        from aether_engine.eq import detect_affect

        user_messages = [
            str(m.get("content"))
            for m in (history or [])
            if isinstance(m, dict) and m.get("role") == "user"
            and isinstance(m.get("content"), str)
        ]
        user_messages.append(prompt)
        return detect_affect(user_messages)
    except Exception:
        logger.debug("Fast-chat EQ read unavailable", exc_info=True)
        return None


async def run_fast_chat(
    prompt: str,
    provider: BaseProvider,
    history: list[dict[str, Any]] | None = None,
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    attachments: list[Any] | None = None,
    workspace_roots: list[str] | None = None,
    user_config=None,
    on_affect_read=None,
    prepared_memory_briefing: str | None = None,
) -> AsyncGenerator[Event, None]:
    """Stream a direct chat reply for *prompt*, mirroring the graph
    runner's event contract (see module docstring)."""
    task_id = task_id or str(uuid.uuid4())
    state_enum = TaskState.PENDING
    validate_transition(state_enum, TaskState.RUNNING)
    state_enum = TaskState.RUNNING

    yield Event(
        type=EventType.TASK_CREATED,
        request_id=request_id,
        payload={
            "task_id": task_id,
            "prompt": prompt,
            "state": state_enum.value,
            "path": "fast_chat",
        },
    )

    # E8-1.1a: step 0 of this task's rewind manifest — the boundary
    # before this exchange. The closing point lands at step 1 on both
    # the completion and failure paths below; a cancelled turn records
    # neither (no partial reply is persisted, matching the runner's
    # cancellation contract).
    await _record_message_point(session_id, task_id, 0, "Before this task began")

    messages: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend(history or [])

    # Calendar layer C3 (14 Nov 2026): fast chat's one-line itinerary
    # — the supervisor gets the full briefing in its prompt; a direct
    # chat reply only needs what's next. Best-effort, empty = absent.
    try:
        from aether_engine.calendar.itinerary import fast_chat_itinerary_line

        _itinerary_line = fast_chat_itinerary_line()
    except Exception:  # noqa: BLE001
        _itinerary_line = ""
    if _itinerary_line:
        messages.append({"role": "system", "content": _itinerary_line})

    # S7 (14 Nov 2026): the memory briefing — profile digest +
    # assembler-scored facts/episodes for this message + lessons,
    # under one small sub-budget (see the S7 block above). It
    # rides beside the itinerary under the same contract:
    # best-effort, empty = absent, never breaks the turn — so
    # text chat and voice (whose talker calls this runner) stop
    # being memory-blind. fast_chat_memory_briefing never raises.
    #
    # S16 (14 Nov 2026): a voice turn may arrive with the briefing
    # already prepared — the session's SpeculativePreparer ran
    # THIS SAME BUILDER on the utterance's stable partial
    # transcript while the user was still speaking, and the
    # preparer's match rule verified the final transcript against
    # the partial before adopting. prepared_memory_briefing is
    # that builder's output, adopted verbatim instead of
    # recomputed: still ONE builder, no fork. None — every typed
    # caller and every unmatched/unprepared voice turn — runs the
    # builder here exactly as pre-S16, byte for byte.
    # Wave 6 (SPEC-6B C6): the turn's memory_used disclosure —
    # the items the briefing below scored into this reply's
    # context, carried out on the TASK_COMPLETED payload. A
    # prepared (speculated) briefing arrives as text only — the
    # preparer keeps no item list — so an adopted briefing
    # discloses an empty list for that turn rather than a
    # reconstructed guess.
    _memory_used: list[dict[str, Any]] = []
    if prepared_memory_briefing is not None:
        _memory_briefing = prepared_memory_briefing
    else:
        _memory_briefing = await fast_chat_memory_briefing(
            prompt, user_config, used_items_out=_memory_used)
    if _memory_briefing:
        messages.append({"role": "system", "content": _memory_briefing})

    # S11 (14 Nov 2026): the EQ briefing — behaviour guidance
    # from the conversation's own signals (see the S11 block
    # above _eq_read_for_turn). Reuses the exact same eq module
    # + briefing as the supervisor path; nothing here forks the
    # detector. Voice rides this runner, so voice turns get the
    # same adaptation, and the read is handed to the pipeline
    # through on_affect_read when a caller supplied one.
    _eq_read = _eq_read_for_turn(prompt, history, user_config)
    if _eq_read is not None:
        from aether_engine.eq import eq_briefing

        _eq_section = eq_briefing(_eq_read)
        if _eq_section:
            messages.append({"role": "system", "content": _eq_section})
        if on_affect_read is not None:
            try:
                on_affect_read(_eq_read)
            except Exception:  # a consumer's callback must never break the turn
                logger.debug("on_affect_read callback failed", exc_info=True)

    start_time = time.time()

    user_content: Any = prompt
    if attachments:
        # S1-M8: every local path is validated (containment + image
        # allow-list + size cap) BEFORE the provider is called; a
        # refusal fails the whole turn visibly — the attachment is
        # never read-and-sent, and never silently dropped either.
        roots = _sanctioned_attachment_roots(workspace_roots)
        parts_list: list[dict[str, Any]] = []
        if prompt:
            parts_list.append({"type": "text", "text": prompt})
        try:
            for att in attachments:
                url = _resolve_attachment_url(att, roots)
                if url:
                    parts_list.append({"type": "image_url", "image_url": {"url": url}})
        except AttachmentRejectedError as e:
            logger.warning("Fast chat attachment refused: %s", e)
            await _record_message_point(session_id, task_id, 1, "Task failed")
            yield Event(
                type=EventType.TASK_FAILED,
                request_id=request_id,
                payload={
                    "task_id": task_id,
                    "state": TaskState.FAILED.value,
                    "error": str(e),
                    "retriable": False,
                    "latency_ms": round((time.time() - start_time) * 1000, 2),
                },
            )
            return
        if parts_list:
            user_content = parts_list

    messages.append({"role": "user", "content": user_content})

    # Calendar layer C2 (14 Nov 2026): if the prompt stated a future
    # event, it has already been stored by the extraction hook; this
    # system note makes the reply announce it (with the undo path)
    # instead of the add happening silently. Best-effort — the hook
    # never raises, and a failure here must not break the turn.
    try:
        from aether_engine.calendar.extraction import note_for_message_if_enabled

        _calendar_note = note_for_message_if_enabled(prompt)
    except Exception:  # noqa: BLE001
        _calendar_note = None
    if _calendar_note:
        messages.append({"role": "system", "content": _calendar_note})

    # S8 (14 Nov 2026): conversational forget, same pattern as the
    # calendar note above — if the prompt is a forget/undo command,
    # the ENGINE has already resolved and deleted (or restored)
    # the memory by the time the model replies; this system note
    # makes the reply announce exactly what happened, including
    # when nothing matched and nothing was deleted. The model
    # itself never deletes. Best-effort — the hook never raises,
    # and a failure here must not break the turn.
    try:
        from aether_engine.memory.forget import (
            note_for_message_if_enabled as forget_note_for_message,
        )

        _forget_note = forget_note_for_message(prompt, user_config=user_config)
    except Exception:  # noqa: BLE001
        _forget_note = None
    if _forget_note:
        messages.append({"role": "system", "content": _forget_note})

    parts: list[str] = []
    try:
        async for chunk in provider.call_stream(messages, tools=None, node_name="fast_chat"):
            if getattr(chunk, "thinking", None):
                # E1-16: thinking streams on its own event type, never
                # inside the answer text.
                yield Event(
                    type=EventType.THINKING,
                    request_id=request_id,
                    payload={
                        "task_id": task_id,
                        "node": "assistant",
                        "text_delta": chunk.thinking,
                    },
                )
            if chunk.text:
                parts.append(chunk.text)
                yield Event(
                    type=EventType.TASK_PROGRESS,
                    request_id=request_id,
                    payload={
                        "task_id": task_id,
                        "text_delta": chunk.text,
                        "full_text": "".join(parts),
                        "turn": 0,
                        "node": "assistant",
                    },
                )
    except asyncio.CancelledError:
        # CANCEL_TASK contract: propagate so execute_task_generator can
        # log the cancellation; no partial reply is persisted, matching
        # the graph path.
        raise
    except Exception as e:  # Provider boundary: any failure becomes a TASK_FAILED event, mirroring the graph runner's contract.
        logger.exception("Fast chat failed")
        await _record_message_point(session_id, task_id, 1, "Task failed")
        yield Event(
            type=EventType.TASK_FAILED,
            request_id=request_id,
            payload={
                "task_id": task_id,
                "state": TaskState.FAILED.value,
                "error": str(e),
                "retriable": _is_retriable_error(e),
                "latency_ms": round((time.time() - start_time) * 1000, 2),
            },
        )
        return

    full_text = "".join(parts).strip()
    await _record_message_point(session_id, task_id, 1, "Reply complete")
    yield Event(
        type=EventType.TASK_COMPLETED,
        request_id=request_id,
        payload={
            "task_id": task_id,
            "state": TaskState.SUCCEEDED.value,
            "latency_ms": round((time.time() - start_time) * 1000, 2),
            "response": full_text or "(task completed)",
            "thoughts": [],
            "session_id": session_id,
            # SPEC-6B C6: the memory items this reply's context
            # actually used (empty when memory was off, nothing
            # scored, or a prepared briefing was adopted).
            "memory_used": _memory_used,
        },
    )
