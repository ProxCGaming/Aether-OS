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
        },
    )
