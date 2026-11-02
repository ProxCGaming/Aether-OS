"""M15 residual (EGM S4 M12 family) — import size caps + format_version.

The landed M15 fix (``f36c83c``) made ZIP import transactional, but
two validation sub-items of the same finding family stayed open:

1. No size/member caps anywhere in the import path — ``zf.read``
   expanded members fully into memory/disk unbounded (zip-bomb
   exposure), and the JSON path read its file unbounded too.
2. ``format_version`` was never validated on either import path —
   both accepted any JSON object with a ``messages`` list, so a
   foreign file in that shape imported as a "session".

These tests pin the caps (member count, per-member expanded size,
total expanded size, session.json size, JSON payload size) and the
version gate (the exporter has always written ``format_version: 1``;
anything else — or none — is not an Aether export and is refused
before any state changes). Caps are monkeypatched to small values
so the tests stay light; the production defaults are asserted sane
in test_caps_defaults_are_generous_but_finite.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from aether_engine.memory import session_store
from aether_engine.session_export import importer
from aether_engine.session_export.importer import (
    import_session_archive,
    import_session_json,
)


@pytest.fixture(autouse=True)
def isolated_session_db(tmp_path, monkeypatch):
    """Isolate session store SQLite database to temporary path for each test."""
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", db_file)
    monkeypatch.setattr(session_store, "_initialized", False)
    session_store.init_db()
    yield db_file


def _zip_bytes(session_payload, attachments: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("session.json", json.dumps(session_payload))
        for name, data in (attachments or {}).items():
            zf.writestr(f"attachments/{name}", data)
    return buf.getvalue()


def _session_payload(**overrides):
    payload = {
        "format_version": 1,
        "id": "caps-test-session",
        "title": "Caps Test",
        "messages": [
            {"role": "user", "content": "hello", "timestamp": 1700000000},
        ],
    }
    payload.update(overrides)
    return payload


class TestFormatVersionValidation:
    def test_zip_rejects_unsupported_format_version(self):
        before = session_store.list_sessions()
        result = import_session_archive(
            content=_zip_bytes(_session_payload(format_version=99)))
        assert result["ok"] is False
        assert "format_version" in result["error"]
        assert "99" in result["error"]
        assert session_store.list_sessions() == before

    def test_zip_rejects_missing_format_version(self):
        payload = _session_payload()
        del payload["format_version"]
        before = session_store.list_sessions()
        result = import_session_archive(content=_zip_bytes(payload))
        assert result["ok"] is False
        assert "format_version" in result["error"]
        assert session_store.list_sessions() == before

    def test_json_rejects_unsupported_format_version(self):
        before = session_store.list_sessions()
        result = import_session_json(
            content=json.dumps(_session_payload(format_version=2)))
        assert result["ok"] is False
        assert "format_version" in result["error"]
        assert session_store.list_sessions() == before

    def test_json_rejects_missing_format_version(self):
        payload = _session_payload()
        del payload["format_version"]
        before = session_store.list_sessions()
        result = import_session_json(content=json.dumps(payload))
        assert result["ok"] is False
        assert "format_version" in result["error"]
        assert session_store.list_sessions() == before

    def test_valid_version_one_imports(self):
        result = import_session_archive(content=_zip_bytes(_session_payload()))
        assert result["ok"] is True
        assert result["message_count"] == 1


class TestZipCaps:
    def test_member_count_cap(self, monkeypatch):
        monkeypatch.setattr(importer, "MAX_IMPORT_MEMBERS", 3)
        before = session_store.list_sessions()
        result = import_session_archive(content=_zip_bytes(
            _session_payload(),
            {"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"},
        ))
        assert result["ok"] is False
        assert "member" in result["error"].lower()
        assert session_store.list_sessions() == before

    def test_member_size_cap(self, monkeypatch):
        monkeypatch.setattr(importer, "MAX_IMPORT_MEMBER_BYTES", 1024)
        before = session_store.list_sessions()
        result = import_session_archive(content=_zip_bytes(
            _session_payload(),
            {"big.bin": b"x" * 2048},
        ))
        assert result["ok"] is False
        assert session_store.list_sessions() == before

    def test_total_expanded_size_cap(self, monkeypatch):
        monkeypatch.setattr(importer, "MAX_IMPORT_TOTAL_BYTES", 1500)
        before = session_store.list_sessions()
        result = import_session_archive(content=_zip_bytes(
            _session_payload(),
            {"a.bin": b"x" * 1000, "b.bin": b"y" * 1000},
        ))
        assert result["ok"] is False
        assert session_store.list_sessions() == before
        # Nothing was placed: the refused import leaves no files.
        assert session_store.get_session("caps-test-session") is None

    def test_session_json_size_cap(self, monkeypatch):
        monkeypatch.setattr(importer, "MAX_SESSION_JSON_BYTES", 64)
        payload = _session_payload()
        payload["messages"] = [
            {"role": "user", "content": "z" * 500, "timestamp": 1700000000},
        ]
        before = session_store.list_sessions()
        result = import_session_archive(content=_zip_bytes(payload))
        assert result["ok"] is False
        assert session_store.list_sessions() == before

    def test_small_import_under_caps_still_works(self, monkeypatch):
        monkeypatch.setattr(importer, "MAX_IMPORT_MEMBERS", 10)
        monkeypatch.setattr(importer, "MAX_IMPORT_TOTAL_BYTES", 100_000)
        result = import_session_archive(content=_zip_bytes(
            _session_payload(),
            {"note.txt": b"hello attachment"},
        ))
        assert result["ok"] is True


class TestJsonCaps:
    def test_json_content_size_cap(self, monkeypatch):
        monkeypatch.setattr(importer, "MAX_IMPORT_JSON_BYTES", 128)
        before = session_store.list_sessions()
        result = import_session_json(
            content=json.dumps(_session_payload()))
        assert result["ok"] is False
        assert session_store.list_sessions() == before

    def test_json_file_size_cap(self, monkeypatch, tmp_path):
        monkeypatch.setattr(importer, "MAX_IMPORT_JSON_BYTES", 128)
        p = tmp_path / "big.json"
        p.write_text(json.dumps(_session_payload()), encoding="utf-8")
        before = session_store.list_sessions()
        result = import_session_json(file_path=p)
        assert result["ok"] is False
        assert session_store.list_sessions() == before


def test_caps_defaults_are_generous_but_finite():
    mib = 1024 * 1024
    assert 100 <= importer.MAX_IMPORT_MEMBERS <= 1_000_000
    assert 16 * mib <= importer.MAX_IMPORT_MEMBER_BYTES <= 4 * 1024 * mib
    assert importer.MAX_IMPORT_MEMBER_BYTES <= importer.MAX_IMPORT_TOTAL_BYTES
    assert importer.MAX_IMPORT_TOTAL_BYTES <= 16 * 1024 * mib
    assert 1 * mib <= importer.MAX_SESSION_JSON_BYTES <= 256 * mib
    assert importer.MAX_SESSION_JSON_BYTES <= importer.MAX_IMPORT_JSON_BYTES
    assert importer.SUPPORTED_FORMAT_VERSIONS == (1,)
