"""Session-side services for the task websocket (Wave 5 split).

Workspace binding, the artifact GET service and message-attachment
registration used to live inline in ``routes/websocket.py``. They
are module-level functions with no per-connection state, so they
moved here wholesale; the websocket endpoint imports and
re-exports every name, so existing import and patch sites keep
resolving against ``aether_engine.routes.websocket`` unchanged.
"""
import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from aether_common.contracts import Event, EventType
from aether_engine.config import find_workspace_root
from aether_engine.memory import session_store
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_sessions")


def _resolve_workspace_binding(
    payload: dict[str, Any], *, required: bool
) -> tuple[bool, str | None, str]:
    """Resolve a request's ``workspace`` field to a session binding.

    The ONE binding rule (per-workspace sessions, grill Q3/Q4),
    shared by SESSION_CREATE, SESSION_MOVE, and — via
    :func:`_create_bound_session` — every implicit creation site,
    so the paths cannot drift apart:

    - field ABSENT: for create (``required=False``), the active
      root — ``workspace_roots[0]``, already canonical in config —
      or None when no roots are configured ("No workspace" is a
      legitimate home, Q4). For move (``required=True``) an absent
      field is a malformed request: a move must name its target.
    - field explicitly null: unbound (None).
    - field a string: canonicalised and matched against the
      configured roots via :func:`find_workspace_root`; the binding
      is the STORED (canonical) root. A string naming no configured
      root is refused — a binding names a workspace (Q2), and
      filing a session under a path the app does not know would
      strand it outside every workspace view.

    Returns (ok, binding, error).
    """
    roots = engine_state.user_config.workspace_roots or []
    if "workspace" not in payload:
        if required:
            return False, None, (
                "workspace is required (a workspace path, or null to unbind)")
        return True, (roots[0] if roots else None), ""
    raw = payload.get("workspace")
    if raw is None:
        return True, None, ""
    if not isinstance(raw, str) or not raw.strip():
        return False, None, "workspace must be a workspace path or null"
    match = find_workspace_root(roots, raw)
    if match is None:
        return False, None, f"workspace is not a configured workspace root: {raw}"
    return True, match, ""


async def _create_bound_session(title: str) -> tuple[str, str | None]:
    """Create a session under the creation binding rule.

    Implicit creation sites (the task pump's first message, the MCP
    prompt/resource saves, voice) carry no workspace field, so they
    take the resolver's absent-field branch: the active root at
    creation time, or unbound when no roots are configured.
    Returns (session_id, binding).
    """
    _ok, binding, _error = _resolve_workspace_binding({}, required=False)
    sess_id = await asyncio.to_thread(session_store.create_session, title, binding)
    return sess_id, binding


async def _unbind_sessions_bound_to(root: str) -> int:
    """Unbind every session filed under ``root`` (grill Q7).

    Workspace removal unbinds — sessions are never deleted by a
    workspace operation. Returns how many sessions were unbound.
    """
    bound = await asyncio.to_thread(session_store.list_sessions, root)
    unbound = 0
    for row in bound:
        if await asyncio.to_thread(
                session_store.set_session_workspace, row["id"], None):
            unbound += 1
    return unbound


async def _workspace_session_counts() -> tuple[dict[str, int], int]:
    """(per-root session counts, unbound count) for WORKSPACE_LIST.

    Keys are the stored canonical roots, exactly as bindings record
    them. A binding naming no configured root (possible only via a
    restored backup) is counted under no row — the session still
    lists in SESSION_LIST; it is simply filed nowhere the Projects
    view knows.
    """
    counts: dict[str, int] = {}
    unbound = 0
    for row in await asyncio.to_thread(session_store.list_sessions):
        binding = row.get("workspace")
        if binding is None:
            unbound += 1
        else:
            counts[binding] = counts.get(binding, 0) + 1
    return counts, unbound


async def _run_mcp_prompt_into_chat(
    name: str, prompt_name: str, arguments: Any, session_id: str | None
) -> dict[str, Any]:
    """Run MCP prompt *prompt_name* on server *name* and insert its
    messages into a chat session (issue 007).

    Shared by the MCP_PROMPT_RUN handler and the ``/mcp:`` slash
    command, so the two entry points cannot drift apart. Returns
    {"ok", "messages", "session_id", "error"}. Arguments are validated
    by the client (dict of strings, never executed). If no session was
    given (or it no longer exists), a session is created so the prompt
    is never lost. A chat-save failure returns an error, never kills
    the websocket.
    """
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

    messages = []
    if not name or not prompt_name:
        return {"ok": False, "messages": [], "session_id": session_id,
                "error": "Server name and prompt name are required."}
    try:
        messages = await GLOBAL_MCP_REGISTRY.get_prompt(name, prompt_name, arguments)
    except Exception as e:  # noqa: BLE001 — MCP client calls raise arbitrary server/transport errors; surfaced to the caller as the error field
        return {"ok": False, "messages": [], "session_id": session_id, "error": str(e)}
    try:
        if session_id:
            sess = await asyncio.to_thread(session_store.get_session, session_id)
            if sess is None:
                session_id = None
        if not session_id:
            session_id, _binding = await _create_bound_session(
                f"MCP prompt: {prompt_name}")
        for msg in messages:
            await asyncio.to_thread(
                session_store.add_message, session_id, msg["role"], msg["content"]
            )
    except Exception as e:  # noqa: BLE001 — chat-persistence boundary: a save failure becomes an error response, never a dead websocket
        logger.error(f"Failed to save MCP prompt to chat: {e}")
        return {"ok": False, "messages": [], "session_id": session_id,
                "error": f"Failed to save prompt to chat: {e}"}
    engine_state.audit_logger.log_event("MCP_PROMPT_RUN", {
        "server": name, "prompt": prompt_name, "session_id": session_id,
        "message_count": len(messages),
    })
    return {"ok": True, "messages": messages, "session_id": session_id, "error": ""}


# S4-N1: one artifact GET used to ship whole files over the socket —
# a large log or image became a giant frame (and a giant renderer
# string). Cap what a single GET returns: text is truncated with a
# marker, and an image over the cap is refused with an honest error
# (truncated base64 would be a corrupt image anyway).
ARTIFACT_CONTENT_CAP_BYTES = 10 * 1024 * 1024
_ARTIFACT_TRUNCATION_MARKER = (
    "\n\n[Truncated: this file exceeds the 10 MB artifact preview cap]"
)


def _read_text_capped(path: Path) -> str:
    """Read at most ARTIFACT_CONTENT_CAP_BYTES of a text file,
    appending the truncation marker when the file is larger (S4-N1)."""
    with path.open("rb") as fh:
        raw = fh.read(ARTIFACT_CONTENT_CAP_BYTES + 1)
    text = raw[:ARTIFACT_CONTENT_CAP_BYTES].decode("utf-8", errors="replace")
    if len(raw) > ARTIFACT_CONTENT_CAP_BYTES:
        text += _ARTIFACT_TRUNCATION_MARKER
    return text


async def handle_artifact_get(artifact_id: str) -> dict[str, Any]:
    """Retrieve an artifact and inspect file existence on disk (Epic 8, E8-2.1)."""
    from aether_engine.artifacts.store import get_artifact_store
    art_store = get_artifact_store()
    art = await asyncio.to_thread(art_store.get_artifact, artifact_id)
    if not art:
        return {
            "artifact": None,
            "content": None,
            "diff": None,
            "exists": False,
            "error": "Artifact not found",
        }

    # E1-04: every disk touch in the inspection (stat, read, the
    # base64 encode of up to the 10 MB preview cap) used to run
    # inline here — on the event loop, inside the receive loop's
    # await — stalling every other event for the duration. It now
    # runs on a worker thread, like the store fetch above.
    diff, content, exists, error = await asyncio.to_thread(
        _inspect_artifact_on_disk, art)

    return {
        "artifact": art,
        "content": content,
        "diff": diff,
        "exists": exists,
        "error": error,
    }


def _inspect_artifact_on_disk(art: dict[str, Any]):
    """Synchronous half of :func:`handle_artifact_get` (E1-04).

    All filesystem IO for one artifact record; runs off the event
    loop. Returns ``(diff, content, exists, error)``.
    """
    kind = art.get("kind", "file")
    file_path = art.get("file_path")
    diff = None
    content = None
    exists = True
    error = None

    if kind == "diff":
        diff = art.get("content")
        if file_path:
            exists = Path(file_path).is_file()
            if not exists:
                error = f"File {file_path} no longer exists on disk"
    elif kind == "file":
        if not file_path or not Path(file_path).is_file():
            exists = False
            error = f"File {file_path} no longer exists on disk" if file_path else "File path not specified"
        else:
            try:
                ext = Path(file_path).suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"):
                    size = Path(file_path).stat().st_size
                    if size > ARTIFACT_CONTENT_CAP_BYTES:
                        error = (
                            f"Image is too large to preview ({size} bytes; "
                            f"the cap is {ARTIFACT_CONTENT_CAP_BYTES} bytes)"
                        )
                    else:
                        import base64
                        b64 = base64.b64encode(Path(file_path).read_bytes()).decode("ascii")
                        # E1-15: .bmp/.ico are in the image
                        # extension list above, so they map to
                        # image MIME types — falling through to
                        # application/octet-stream made their
                        # previews impossible to render as images.
                        mime = (
                            "image/png" if ext == ".png"
                            else "image/jpeg" if ext in (".jpg", ".jpeg")
                            else "image/gif" if ext == ".gif"
                            else "image/webp" if ext == ".webp"
                            else "image/bmp" if ext == ".bmp"
                            else "image/x-icon" if ext == ".ico"
                            else "application/octet-stream"
                        )
                        content = f"data:{mime};base64,{b64}"
                else:
                    content = _read_text_capped(Path(file_path))
            except Exception as read_err:  # noqa: BLE001
                exists = False
                error = f"Error reading file: {read_err}"
    else:  # report, html, etc.
        if file_path and Path(file_path).is_file():
            try:
                content = _read_text_capped(Path(file_path))
            except Exception as read_err:  # noqa: BLE001
                error = f"Error reading file: {read_err}"
        else:
            content = art.get("content")

    return diff, content, exists, error


def _remove_session_attachments(session_id: str) -> None:
    """Remove a session's materialised attachments directory (S4-M3).

    Attachments/snips are written under
    ``~/.aether/attachments/<session_id>/`` by
    ``_register_message_attachments``; deleting the session must
    remove that directory too, not just its DB rows. Best-effort:
    failures are logged, never raised.
    """
    try:
        attach_dir = Path.home() / ".aether" / "attachments" / session_id
        shutil.rmtree(attach_dir, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to remove attachments for session {session_id}: {e}")


async def _register_message_attachments(
    session_id: str | None,
    message_id: str | None,
    attachments: list[Any] | None,
    ws: WebSocket | None = None,
) -> None:
    """Auto-register user message attachments into MediaStore in place (Epic 8, E8-4.2)."""
    if not attachments:
        return
    try:
        import base64

        from aether_engine.media.store import get_media_store
        store = get_media_store()
        attach_dir = Path.home() / ".aether" / "attachments" / (session_id or "global")

        for att in attachments:
            file_path = None
            origin = "user_upload"
            mime_type = None

            if isinstance(att, dict):
                file_path = att.get("path")
                data_url = att.get("data") or att.get("url")
                mime_type = att.get("mime_type")
                name = att.get("name") or "attachment"

                if "snip" in name.lower() or att.get("bounds"):
                    origin = "snip"

                if not file_path and data_url and isinstance(data_url, str) and data_url.startswith("data:"):
                    attach_dir.mkdir(parents=True, exist_ok=True)
                    header, _, b64_str = data_url.partition(",")
                    if ";base64" in header:
                        extracted_mime = header.split(":")[1].split(";")[0]
                        mime_type = mime_type or extracted_mime
                    ext = ".png"
                    if mime_type and "jpeg" in mime_type:
                        ext = ".jpg"
                    elif mime_type and "webp" in mime_type:
                        ext = ".webp"
                    # E1-02: the client-supplied name is reduced to
                    # its final path component before it joins the
                    # filesystem path. A name carrying '/' or '\'
                    # separators (a traversal attempt, or just a
                    # chatty client) otherwise made the write raise
                    # or escape the session's attachments directory.
                    safe_name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
                    if safe_name in ("", ".", ".."):
                        safe_name = "attachment"
                    target_file = attach_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
                    if not target_file.suffix:
                        target_file = target_file.with_suffix(ext)
                    target_file.write_bytes(base64.b64decode(b64_str))
                    file_path = str(target_file)
            elif isinstance(att, str):
                if att.startswith("data:"):
                    attach_dir.mkdir(parents=True, exist_ok=True)
                    header, _, b64_str = att.partition(",")
                    target_file = attach_dir / f"{uuid.uuid4().hex[:8]}_image.png"
                    target_file.write_bytes(base64.b64decode(b64_str))
                    file_path = str(target_file)
                else:
                    file_path = att

            if file_path:
                ext = Path(file_path).suffix.lower()
                is_img = ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
                is_vid = ext in (".mp4", ".webm", ".mkv", ".mov", ".avi")
                if is_img or is_vid or (mime_type and mime_type.startswith(("image/", "video/"))):
                    media_type = "video" if (is_vid or (mime_type and mime_type.startswith("video/"))) else "image"
                    item = await asyncio.to_thread(
                        store.register_media,
                        file_path=file_path,
                        collection="user",
                        media_type=media_type,
                        session_id=session_id,
                        message_id=message_id,
                        mime_type=mime_type,
                        probe_metadata={"origin": origin, "name": Path(file_path).name},
                    )
                    if ws:
                        try:
                            await ws.send_text(Event(
                                type=EventType.MEDIA_RECORDED,
                                payload=item,
                            ).to_json())
                        except Exception:  # noqa: BLE001, S110
                            pass
                    # Epic 8, E8-4.3: analyse on arrival — detached
                    # background task, never blocks the message flow.
                    try:
                        from aether_engine.media.analyzer import schedule_media_analysis

                        schedule_media_analysis(item["id"])
                    except Exception:  # noqa: BLE001, S110
                        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to auto-register message attachments into MediaStore: {e}")
