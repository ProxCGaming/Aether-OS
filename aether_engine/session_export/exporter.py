"""Session export serializers for Markdown, JSON, and ZIP formats (Epic 8, E8-A7.1).

Integrates with the diagnostics redactor to guarantee that no secret patterns
or sensitive keys are exported. That guarantee covers ZIP attachments too:
a UTF-8 text attachment is scrubbed with the same redactor before it is
written to the archive, and a binary attachment — whose content cannot be
decoded as text and therefore cannot be verified for redaction — is never
copied byte-identical; it is omitted from the ZIP and listed by name in the
``attachments/OMITTED.txt`` manifest entry.
"""

from __future__ import annotations

import json
import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Any

from aether_engine.audit import _is_sensitive_key, _scrub_string
from aether_engine.memory import session_store

logger = logging.getLogger("aether_engine.session_export.exporter")


def data_home() -> Path:
    return Path.home() / ".aether"


def attachments_dir(session_id: str | None = None) -> Path:
    base = data_home() / "attachments"
    if session_id:
        return base / session_id
    return base


def exports_dir() -> Path:
    d = data_home() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def redact_data(val: Any) -> Any:
    """Recursively redact secrets and sensitive keys using the diagnostics redactor."""
    if isinstance(val, dict):
        out = {}
        for k, v in val.items():
            if _is_sensitive_key(str(k)):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_data(v)
        return out
    if isinstance(val, (list, tuple)):
        return [redact_data(v) for v in val]
    if isinstance(val, str):
        return _scrub_string(val)
    return val


def _safe_filename(title: str, session_id: str, ext: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", title).strip()
    slug = re.sub(r"[-\s]+", "-", cleaned)[:30].strip("-") or "session"
    suffix = session_id[:8] if session_id else "export"
    return f"{slug}-{suffix}.{ext.lstrip('.')}"


def _format_timestamp(ts: float | None) -> str:
    if not ts:
        return "Unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def export_session_markdown(session_id: str) -> str:
    """Generate a readable Markdown transcript including messages, tool calls, and results."""
    sess = session_store.get_session(session_id)
    if not sess:
        raise ValueError(f"Session {session_id} not found")

    sess = redact_data(sess)
    title = sess.get("title", "Untitled Session")
    created_at = sess.get("created_at")
    updated_at = sess.get("updated_at")
    messages = sess.get("messages", [])

    # Provenance label only (grill Q9, superseded form): the export
    # records where the session lived so a reader can see it, but the
    # binding is not a filing instruction — import always lands the
    # session unbound (see the importer).
    workspace = sess.get("workspace")
    workspace_line = (
        f"- **Workspace:** `{workspace}`" if workspace else "- **Workspace:** No workspace"
    )

    lines: list[str] = [
        f"# Session: {title}",
        "",
        f"- **Session ID:** `{session_id}`",
        f"- **Created:** {_format_timestamp(created_at)}",
        f"- **Updated:** {_format_timestamp(updated_at)}",
        f"- **Messages:** {len(messages)}",
        workspace_line,
        "",
        "---",
        "",
    ]

    for msg in messages:
        role = (msg.get("role") or "unknown").capitalize()
        ts = msg.get("timestamp")
        content = (msg.get("content") or "").strip()
        lines.append(f"## {role}")
        if ts:
            lines.append(f"*{_format_timestamp(ts)}*")
        lines.append("")

        if content:
            lines.append(content)
            lines.append("")

        # Attachments
        attachments = msg.get("attachments")
        if attachments:
            lines.append("### Attachments")
            if isinstance(attachments, list):
                for att in attachments:
                    name = att.get("name") if isinstance(att, dict) else str(att)
                    lines.append(f"- `{name}`")
            lines.append("")

        # Tool activity, approvals, node transitions, and thinking
        thoughts = msg.get("thoughts")
        if thoughts and isinstance(thoughts, list):
            for t in thoughts:
                if not isinstance(t, dict):
                    continue
                t_type = t.get("type")
                if t_type == "TOOL_ACTIVITY":
                    payload = t.get("payload", {})
                    t_name = payload.get("tool_name", "unknown_tool")
                    status = payload.get("status", "unknown")
                    args = payload.get("tool_args", {})
                    result = payload.get("result")
                    err = payload.get("error")

                    lines.append(f"> **Tool Call:** `{t_name}` (status: {status})")
                    if args:
                        lines.append(f"> **Arguments:** `{json.dumps(args)}`")
                    if result is not None:
                        res_str = str(result)
                        lines.append(f"> **Result:** {res_str}")
                    if err:
                        lines.append(f"> **Error:** {err}")
                    lines.append(">")
                elif t_type == "NODE_ACTIVITY":
                    payload = t.get("payload", {})
                    from_node = payload.get("from_node", "node")
                    to_node = payload.get("to_node", "node")
                    lines.append(f"> **Node Transition:** `{from_node}` → `{to_node}`")
                    lines.append(">")
                elif t.get("text"):
                    node = t.get("node", "thought")
                    lines.append(f"> **Thinking ({node}):** {t['text']}")
                    lines.append(">")
            lines.append("")

        lines.append("---")
        lines.append("")

    raw_markdown = "\n".join(lines)
    return _scrub_string(raw_markdown)


def export_session_json(session_id: str) -> str:
    """Generate a full-fidelity redacted JSON export of the session."""
    sess = session_store.get_session(session_id)
    if not sess:
        raise ValueError(f"Session {session_id} not found")

    redacted = redact_data(sess)
    payload = {
        "format_version": 1,
        "exported_at": int(time.time()),
        **redacted,
    }
    dumped = json.dumps(payload, indent=2, ensure_ascii=False)
    return _scrub_string(dumped)


def _write_attachment_scrubbed(
    zf: zipfile.ZipFile, path: Path, arcname: str
) -> bool:
    """Write one attachment to ``zf`` only if it can be redaction-verified.

    Reads the file's bytes. If they decode as UTF-8 text, the text is run
    through ``_scrub_string`` and the scrubbed text is written at
    ``arcname``. If they do not decode as text (binary content), the bytes
    are NOT copied: binary content cannot be checked for embedded secrets,
    so copying it raw would bypass the redaction applied to session.json
    and transcript.md.

    Returns True if the (scrubbed) attachment was written, False if it was
    omitted as unverifiable binary content (or could not be read).
    """
    try:
        raw = path.read_bytes()
    except OSError:
        logger.warning("Could not read attachment %s; omitting from export", path)
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    zf.writestr(arcname, _scrub_string(text).encode("utf-8"))
    return True


def export_session_zip(session_id: str, dest_path: Path | str | None = None) -> Path:
    """Package session JSON, readable Markdown transcript, and attachments into a ZIP archive."""
    sess = session_store.get_session(session_id)
    if not sess:
        raise ValueError(f"Session {session_id} not found")

    title = sess.get("title", "Session")
    if dest_path is None:
        filename = _safe_filename(title, session_id, "zip")
        dest = exports_dir() / filename
    else:
        dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    json_content = export_session_json(session_id)
    md_content = export_session_markdown(session_id)

    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("session.json", json_content.encode("utf-8"))
        zf.writestr("transcript.md", md_content.encode("utf-8"))

        # Collect attachments from disk. Every attachment goes through
        # _write_attachment_scrubbed: text is included scrubbed, binary is
        # omitted (never copied raw) and recorded for the manifest below.
        omitted: list[str] = []
        seen_arcnames: set[str] = set()

        att_dir = attachments_dir(session_id)
        if att_dir.exists() and att_dir.is_dir():
            for f in sorted(att_dir.iterdir()):
                if f.is_file():
                    arcname = f"attachments/{f.name}"
                    if arcname in seen_arcnames:
                        continue
                    seen_arcnames.add(arcname)
                    if not _write_attachment_scrubbed(zf, f, arcname):
                        omitted.append(arcname)

        # Also collect any message attachments referenced by absolute or relative path
        messages = sess.get("messages", [])
        for m in messages:
            att_list = m.get("attachments")
            if isinstance(att_list, list):
                for att in att_list:
                    if isinstance(att, dict) and "path" in att:
                        p = Path(att["path"])
                        name = att.get("name") or p.name
                        arcname = f"attachments/{name}"
                        if p.exists() and p.is_file() and arcname not in seen_arcnames:
                            seen_arcnames.add(arcname)
                            if not _write_attachment_scrubbed(zf, p, arcname):
                                omitted.append(arcname)

        if omitted:
            manifest = (
                "The following attachments were omitted from this export "
                "because they are binary files and binary content cannot be "
                "verified for redaction:\n"
                "\n"
                + "".join(f"- {name}\n" for name in omitted)
                + "\n"
                "Binary attachments are withheld rather than copied "
                "byte-identical, so that no unredacted secret embedded in "
                "them can leak through the export.\n"
            )
            manifest_arcname = "attachments/OMITTED.txt"
            if manifest_arcname in seen_arcnames:
                # A real attachment already occupies the manifest name.
                manifest_arcname = "attachments/OMITTED-manifest.txt"
            zf.writestr(manifest_arcname, manifest.encode("utf-8"))

    return dest
