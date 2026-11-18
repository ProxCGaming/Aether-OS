"""Skills registry workflow for the task websocket (Wave 5, D4-6).

The Settings panel's front-end over the Epic 7 skills CLI engine
(``aether_engine.skills.skill_registry``): search one NAMED source,
prepare one skill (fetch + hash, nothing written — the pin summary
the install card shows), and install it against the reviewed hash.

Scope is the CLI's user scope, resolved the same way: the lockfile
sits beside the skill manager's global root
(``~/.aether/skills.lock.json``) and installs materialise into that
root (``~/.aether/skills``), so what the panel installs is exactly
what the manager lists. Relative local locators resolve against
the lockfile's directory, as the CLI resolves them.

Reply discipline matches the sibling families: work runs in a
background task (a catalogue scan / fetch / install copies files),
and every request is answered — a source that cannot be read is a
``success: False`` payload carrying the engine's own reason (the
CLI's honesty, e.g. a github source in this build), never silence
and never a fabricated empty result.
"""
import asyncio
from pathlib import Path

from aether_common.contracts import EventType
from aether_engine.skills import skill_registry
from aether_engine.skills.lockfile import LOCKFILE_NAME
from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER


def _user_scope() -> tuple[Path, Path]:
    """(lockfile, skills root) for the user scope, CLI-style."""
    skills_root = Path(GLOBAL_SKILL_MANAGER.global_dir)
    return skills_root.parent / LOCKFILE_NAME, skills_root


async def send_skill_search_result(conn, req_id_, source_type_, locator_, query_):
    """Answer a SKILL_SEARCH_REQUEST from a background task."""

    async def _build_payload():
        lockfile, _root = _user_scope()
        entries = await asyncio.to_thread(
            skill_registry.search_catalogue,
            source_type_, locator_, query_,
            base_dir=lockfile.parent)
        return {
            "success": True,
            "entries": entries,
            "source_type": source_type_,
            "source": locator_,
            "error": None,
        }

    await conn.reply(
        req_id_, EventType.SKILL_SEARCH_RESPONSE, _build_payload,
        error_payload=lambda e: {
            "success": False, "entries": [],
            "source_type": source_type_, "source": locator_,
            "error": str(e)},
        send_failure_message="Could not send skill search response")


async def send_skill_install_prepare_result(conn, req_id_, source_type_, locator_, name_):
    """Answer a SKILL_INSTALL_PREPARE_REQUEST from a background task.

    Prepare fetches + hashes and writes NOTHING: the returned pin
    summary is what the install card shows and what the eventual
    install is checked against.
    """

    async def _build_payload():
        lockfile, _root = _user_scope()
        pin = await asyncio.to_thread(
            skill_registry.prepare_named,
            name_, source_type_, locator_,
            base_dir=lockfile.parent)
        return {"success": True, "pin": pin, "error": None}

    await conn.reply(
        req_id_, EventType.SKILL_INSTALL_PREPARE_RESPONSE, _build_payload,
        error_payload=lambda e: {
            "success": False, "name": name_, "pin": None,
            "error": str(e)},
        send_failure_message="Could not send skill install prepare response")


async def send_skill_install_result(conn, req_id_, source_type_, locator_, name_, expected_hash_):
    """Answer a SKILL_INSTALL_REQUEST from a background task.

    Installs through ``install_named`` with the hash the user
    reviewed; a fresh install then lands DISABLED (D4-6 — enabling
    is the separate SKILL_TOGGLE act), and the reply carries the
    refreshed, pin-annotated list in the toggle reply's shape.
    """

    async def _build_payload():
        lockfile, skills_root = _user_scope()
        source = await asyncio.to_thread(
            skill_registry.open_source, source_type_, locator_,
            base_dir=lockfile.parent)
        outcome = await asyncio.to_thread(
            skill_registry.install_named,
            name_, source,
            source_type=source_type_, source_locator=locator_,
            lockfile_path=lockfile, skills_root=skills_root,
            expected_hash=expected_hash_)
        payload = {
            "success": outcome.ok,
            "name": name_,
            "action": outcome.action,
            "detail": outcome.detail,
            "error": None if outcome.ok else outcome.detail,
        }
        if outcome.ok:
            # Mirror plugin install: the live index refreshes after
            # the commit, never before it.
            GLOBAL_SKILL_MANAGER.refresh()
            if outcome.action == "installed":
                # Lands disabled. A failed disable save must not be
                # dressed up: the install stands, and the reply says
                # the skill is live instead of claiming otherwise.
                try:
                    GLOBAL_SKILL_MANAGER.set_disabled(name_, True)
                except Exception as e:  # noqa: BLE001 — reported in the payload, per the toggle family's discipline
                    payload["error"] = (
                        f"Installed, but the disabled state could "
                        f"not be saved, so the skill is currently "
                        f"enabled: {e}")
            payload["skills"] = skill_registry.annotated_skill_dicts(
                GLOBAL_SKILL_MANAGER)
        return payload

    await conn.reply(
        req_id_, EventType.SKILL_INSTALL_RESPONSE, _build_payload,
        error_payload=lambda e: {
            "success": False, "name": name_, "error": str(e)},
        send_failure_message="Could not send skill install response")
