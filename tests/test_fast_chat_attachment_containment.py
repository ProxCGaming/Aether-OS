"""S1-M8: fast chat reads arbitrary local paths as attachments.

``run_fast_chat`` resolved a client-supplied attachment ``path`` with a
bare ``Path(...).read_bytes()`` and shipped the bytes to the cloud
provider as image content — no containment, no mime allow-list, no
size cap. A websocket client could attach ``/etc/passwd`` (or any
readable local file) and have its contents leave the machine.

Pinned here:
  * a path outside the sanctioned roots (the session attachments
    directory and the granted workspace roots — the same boundary
    ``tools/paths.py`` enforces for tool paths) is refused, the turn
    fails visibly with TASK_FAILED, and the bytes NEVER reach the
    provider call;
  * a file inside the session attachments dir loads and is sent
    exactly as before, as does a file inside a granted workspace root;
  * a non-image file inside the roots is refused (extension and
    declared mime must both be on the image allow-list);
  * an oversize image inside the roots is refused;
  * a symlink inside a root that points outside it is refused
    (containment is judged on the resolved path, like the tools);
  * data-URL and http(s) attachments keep their existing pass-through
    behaviour.

All providers are scripted stubs (house pattern): no network.
"""
import base64

import pytest

from aether_common.contracts import EventType, TaskState
from aether_engine.langgraph import fast_chat
from aether_engine.langgraph.fast_chat import run_fast_chat
from aether_engine.providers.base import StreamChunk

# 1x1 transparent PNG (same fixture bytes as test_vision_attachment_flow).
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_B64)
PNG_DATA_URL = f"data:image/png;base64,{PNG_B64}"


class RecordingProvider:
    """Records every message list it is shown; replies with one chunk."""

    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        yield StreamChunk(text="ok", finish_reason="stop")


async def _collect(agen):
    return [event async for event in agen]


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Sanctioned roots isolated to tmp: the session attachments dir
    (via the module's attachments-root helper) and one granted
    workspace root passed explicitly, as the websocket handler does."""
    attachments_root = tmp_path / "attachments"
    session_dir = attachments_root / "sess-1"
    session_dir.mkdir(parents=True)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(fast_chat, "_attachments_root", lambda: attachments_root)
    return {
        "attachments_root": attachments_root,
        "session_dir": session_dir,
        "workspace_root": workspace_root,
        "outside_dir": tmp_path / "outside",
    }


def _user_content(provider):
    messages = provider.calls[0]
    user = [m for m in messages if m["role"] == "user"]
    assert len(user) == 1
    return user[0]["content"]


def _image_urls(provider):
    content = _user_content(provider)
    if not isinstance(content, list):
        return []
    return [
        part["image_url"]["url"]
        for part in content
        if part.get("type") == "image_url"
    ]


# ---------------------------------------------------------------------------
# (1) Outside the sanctioned roots: refused, bytes never sent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_outside_roots_is_refused_and_never_sent(roots, tmp_path):
    secret = tmp_path / "outside" / "secret.png"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(PNG_BYTES)

    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "what is this?",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[{"path": str(secret), "mime_type": "image/png"}],
    ))

    assert events[0].type == EventType.TASK_CREATED
    assert events[-1].type == EventType.TASK_FAILED
    assert events[-1].payload["state"] == TaskState.FAILED.value
    assert events[-1].payload["retriable"] is False
    assert "attachment" in events[-1].payload["error"].lower()
    assert provider.calls == [], "a refused attachment must never reach the provider"


@pytest.mark.asyncio
async def test_system_file_path_is_refused_and_never_sent(roots):
    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "read this",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=["/etc/passwd"],
    ))

    assert events[-1].type == EventType.TASK_FAILED
    assert provider.calls == []


@pytest.mark.asyncio
async def test_symlink_inside_root_pointing_outside_is_refused(roots, tmp_path):
    outside = tmp_path / "outside" / "real.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(PNG_BYTES)
    link = roots["session_dir"] / "link.png"
    link.symlink_to(outside)

    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "what is this?",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[{"path": str(link), "mime_type": "image/png"}],
    ))

    assert events[-1].type == EventType.TASK_FAILED
    assert provider.calls == []


# ---------------------------------------------------------------------------
# (2) Inside the sanctioned roots: loads and is sent as before
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_inside_session_attachments_dir_is_sent(roots):
    img = roots["session_dir"] / "snip.png"
    img.write_bytes(PNG_BYTES)

    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "what is this?",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[{"path": str(img), "mime_type": "image/png", "name": "snip.png"}],
    ))

    assert events[-1].type == EventType.TASK_COMPLETED
    assert _image_urls(provider) == [PNG_DATA_URL]


@pytest.mark.asyncio
async def test_bare_string_path_inside_workspace_root_is_sent_with_real_mime(roots):
    img = roots["workspace_root"] / "photo.jpg"
    img.write_bytes(PNG_BYTES)

    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "describe it",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[str(img)],
    ))

    assert events[-1].type == EventType.TASK_COMPLETED
    # The mime now comes from the file itself, not a hardcoded image/png.
    assert _image_urls(provider) == [
        f"data:image/jpeg;base64,{PNG_B64}"]


# ---------------------------------------------------------------------------
# (3) Non-image inside the roots: refused
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_image_inside_roots_is_refused(roots):
    note = roots["session_dir"] / "notes.txt"
    note.write_text("not an image", encoding="utf-8")

    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "read this",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[{"path": str(note), "mime_type": "image/png"}],
    ))

    assert events[-1].type == EventType.TASK_FAILED
    assert "attachment" in events[-1].payload["error"].lower()
    assert provider.calls == []


@pytest.mark.asyncio
async def test_image_with_non_image_declared_mime_is_refused(roots):
    img = roots["session_dir"] / "snip.png"
    img.write_bytes(PNG_BYTES)

    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "what is this?",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[{"path": str(img), "mime_type": "text/plain"}],
    ))

    assert events[-1].type == EventType.TASK_FAILED
    assert provider.calls == []


# ---------------------------------------------------------------------------
# (4) Oversize image inside the roots: refused
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oversize_image_inside_roots_is_refused(roots, monkeypatch):
    monkeypatch.setattr(fast_chat, "MAX_ATTACHMENT_BYTES", 16)
    img = roots["session_dir"] / "big.png"
    img.write_bytes(PNG_BYTES)  # the 1x1 PNG is already > 16 bytes

    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "what is this?",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[{"path": str(img), "mime_type": "image/png"}],
    ))

    assert events[-1].type == EventType.TASK_FAILED
    assert "attachment" in events[-1].payload["error"].lower()
    assert provider.calls == []


# ---------------------------------------------------------------------------
# Existing pass-through behaviour is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_data_url_attachment_passes_through(roots):
    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "what is this?",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[{"name": "snip.png", "mime_type": "image/png", "data": PNG_DATA_URL}],
    ))

    assert events[-1].type == EventType.TASK_COMPLETED
    assert _image_urls(provider) == [PNG_DATA_URL]


@pytest.mark.asyncio
async def test_http_url_attachment_passes_through(roots):
    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "what is this?",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=["https://example.com/pic.png"],
    ))

    assert events[-1].type == EventType.TASK_COMPLETED
    assert _image_urls(provider) == ["https://example.com/pic.png"]


@pytest.mark.asyncio
async def test_missing_file_inside_roots_fails_visibly_not_silently(roots):
    missing = roots["session_dir"] / "gone.png"

    provider = RecordingProvider()
    events = await _collect(run_fast_chat(
        "what is this?",
        provider,
        session_id="sess-1",
        workspace_roots=[str(roots["workspace_root"])],
        attachments=[{"path": str(missing), "mime_type": "image/png"}],
    ))

    assert events[-1].type == EventType.TASK_FAILED
    assert provider.calls == []
