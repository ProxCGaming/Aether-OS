"""Session export serializers for Markdown, JSON, and ZIP formats (Epic 8, E8-A7.1).

Integrates with the diagnostics redactor to guarantee that no secret patterns
or sensitive keys are exported.
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

    lines: list[str] = [
        f"# Session: {title}",
        "",
        f"- **Session ID:** `{session_id}`",
        f"- **Created:** {_format_timestamp(created_at)}",
        f"- **Updated:** {_format_timestamp(updated_at)}",
        f"- **Messages:** {len(messages)}",
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

        # Collect attachments from disk
        att_dir = attachments_dir(session_id)
        if att_dir.exists() and att_dir.is_dir():
            for f in sorted(att_dir.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f"attachments/{f.name}")

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
                        if p.exists() and p.is_file() and arcname not in zf.namelist():
                            zf.write(p, arcname=arcname)

    return dest
