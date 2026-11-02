"""S1-M8-ADJ: the graph path's shared attachment resolver lacks the
containment S1-M8 added to fast chat.

``langgraph/state.py::_attachment_image_url`` (used by
``multimodal_user_content`` for graph tasks and steering) resolved a
file ``path`` with a bare ``Path(path).read_bytes()`` — no containment,
no mime allow-list, no size cap — so ANY local file became a ``data:``
URL for the model. The fix routes that branch through fast chat's
S1-M8 loader (``_load_path_attachment`` against
``_sanctioned_attachment_roots``), degrading a refusal to the graph
path's existing "could not be loaded" note instead of raising.

Pinned here:
  * a path outside the sanctioned roots yields the degrade note and
    NO image part (before the fix it was inlined);
  * a real PNG inside the engine attachments tree yields an image
    part with a data: URL;
  * a non-image file inside a granted workspace root is refused;
  * an oversized image inside the roots is refused;
  * an inline ``data:`` URL passes through unchanged.

No network, no providers: ``multimodal_user_content`` is called
directly, exactly as the executor and steering paths call it.
"""
import base64
from pathlib import Path

from aether_engine.langgraph.state import multimodal_user_content

# 1x1 transparent PNG (same fixture bytes as the other attachment tests).
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_B64)
PNG_DATA_URL = f"data:image/png;base64,{PNG_B64}"
PROMPT = "What does this show?"


def _image_urls(content):
    if not isinstance(content, list):
        return []
    return [
        part["image_url"]["url"]
        for part in content
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


def _text_notes(content):
    if not isinstance(content, list):
        return []
    return [
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]


def _assert_degraded_to_note(content, label):
    assert _image_urls(content) == [], (
        f"refused attachment must not become an image part: {content!r}")
    notes = " ".join(_text_notes(content))
    assert label in notes and "could not be loaded" in notes, (
        f"refused attachment must degrade to the honest note, got {content!r}")


# ---------------------------------------------------------------------------
# (a) Outside the sanctioned roots: refused — note, no image part
# ---------------------------------------------------------------------------

def test_path_outside_sanctioned_roots_degrades_to_note(tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG_BYTES)

    content = multimodal_user_content(PROMPT, [
        {"name": "secret.png", "mime_type": "image/png",
         "path": str(outside)},
    ])

    _assert_degraded_to_note(content, "secret.png")


# ---------------------------------------------------------------------------
# (b) Inside the attachments tree: loads as an image part
# ---------------------------------------------------------------------------

def test_png_inside_attachments_tree_is_sent():
    # conftest sandboxes HOME; fast_chat._attachments_root() is
    # Path.home() / ".aether" / "attachments".
    session_dir = Path.home() / ".aether" / "attachments" / "sess-s1m8adj"
    session_dir.mkdir(parents=True, exist_ok=True)
    img = session_dir / "snip.png"
    img.write_bytes(PNG_BYTES)

    content = multimodal_user_content(PROMPT, [
        {"name": "snip.png", "mime_type": "image/png", "path": str(img)},
    ])

    assert _image_urls(content) == [PNG_DATA_URL]


# ---------------------------------------------------------------------------
# (c) Non-image inside a granted workspace root: refused
# ---------------------------------------------------------------------------

def test_non_image_inside_granted_workspace_root_is_refused(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    note = workspace_root / "notes.txt"
    note.write_text("not an image", encoding="utf-8")

    content = multimodal_user_content(
        PROMPT,
        [{"name": "notes.txt", "mime_type": "image/png",
          "path": str(note)}],
        workspace_roots=[str(workspace_root)],
    )

    _assert_degraded_to_note(content, "notes.txt")


# ---------------------------------------------------------------------------
# (d) Oversized image inside the roots: refused
# ---------------------------------------------------------------------------

def test_oversized_image_inside_roots_is_refused(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    big = workspace_root / "big.png"
    # Sparse file just over the 10 MB cap (loader judges by stat size).
    with big.open("wb") as fh:
        fh.truncate(10 * 1024 * 1024 + 1)

    content = multimodal_user_content(
        PROMPT,
        [{"name": "big.png", "mime_type": "image/png", "path": str(big)}],
        workspace_roots=[str(workspace_root)],
    )

    _assert_degraded_to_note(content, "big.png")


# ---------------------------------------------------------------------------
# (e) Inline data: URL passes through unchanged (regression pin)
# ---------------------------------------------------------------------------

def test_inline_data_url_passes_through_unchanged():
    content = multimodal_user_content(PROMPT, [
        {"name": "snip.png", "mime_type": "image/png", "data": PNG_DATA_URL},
    ])

    assert _image_urls(content) == [PNG_DATA_URL]
