"""Epic 7 — plugins & skills: hooks, TTSR, skill disable,
auto-match + disclosure, and plugin toggle teardown.

Acceptance highlights:
- A hook can block / rewrite a tool call and replace a result;
  a throwing hook fails closed. (E7-01)
- TTSR rules redact or block streamed output; off by default;
  invalid rules are refused at config load. (E7-02)
- A disabled skill vanishes from every agent-facing path —
  list-for-agents semantics are the index: slash resolution,
  auto-match, content load — while Settings still sees it with
  enabled=False. (E7-03)
- Auto-match activates a keyword skill on a plain prompt, and the
  supervisor catalogue carries summaries, never bodies. (E7-04)
- Disabling a plugin removes its MCP servers and hides its skills;
  enabling restores both. (E7-05)
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from aether_engine.tools import hooks
from aether_engine.tools.hooks import HookBlock

# ---------------------------------------------------------------------------
# E7-01 — hooks
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


class _Audit:
    def __init__(self):
        self.events = []

    def log_event(self, name, payload):
        self.events.append((name, payload))


def test_hook_can_block_and_audits():
    audit = _Audit()
    hooks.register_tool_call_hook("no-shell", lambda t, a, c: (_ for _ in ()).throw(HookBlock("shell is off")))
    with pytest.raises(HookBlock):
        hooks.apply_tool_call_hooks("run_shell", {"command": "ls"}, audit_logger=audit)
    assert any(e == "HOOK_BLOCKED" for e, _ in audit.events)


def test_hook_can_rewrite_args():
    hooks.register_tool_call_hook("force-ls", lambda t, a, c: {**a, "command": "ls"})
    out = hooks.apply_tool_call_hooks("run_shell", {"command": "rm -rf /"})
    assert out["command"] == "ls"


def test_throwing_call_hook_fails_closed():
    def boom(t, a, c):
        raise RuntimeError("buggy hook")

    hooks.register_tool_call_hook("boom", boom)
    with pytest.raises(HookBlock):
        hooks.apply_tool_call_hooks("read_file", {"path": "x"})


def test_result_hook_replaces_and_throwing_hook_withholds():
    hooks.register_tool_result_hook("tag", lambda t, a, r, c: r + " [tagged]")
    assert hooks.apply_tool_result_hooks("read_file", {}, "body") == "body [tagged]"
    hooks.clear_hooks()

    def boom(t, a, r, c):
        raise RuntimeError("veto")

    hooks.register_tool_result_hook("boom", boom)
    out = hooks.apply_tool_result_hooks("read_file", {}, "secret body")
    assert "secret body" not in out
    assert "withheld" in out


def test_hooks_run_inside_the_real_pipeline():
    from aether_engine.tools.registry import ToolExecutionError, build_tool_registry

    registry = build_tool_registry()
    hooks.register_tool_call_hook("block-time", lambda t, a, c: (_ for _ in ()).throw(HookBlock("no clock")))
    with pytest.raises(ToolExecutionError):
        asyncio.run(registry.execute("get_current_time", {}, workspace_roots=[]))


# ---------------------------------------------------------------------------
# E7-02 — TTSR
# ---------------------------------------------------------------------------


def test_ttsr_cleaning_drops_invalid_rules():
    from aether_engine.providers.ttsr import clean_ttsr_rules

    cleaned = clean_ttsr_rules([
        {"name": "ok", "pattern": "sk-[0-9]+", "action": "redact"},
        {"name": "bad-regex", "pattern": "([", "action": "redact"},
        {"name": "bad-action", "pattern": "x", "action": "explode"},
        {"pattern": "noname"},
        "junk",
    ])
    assert [r["name"] for r in cleaned] == ["ok"]


def test_ttsr_redact_and_block():
    from aether_engine.providers.ttsr import StreamRules

    rules = StreamRules([
        {"name": "apikey", "pattern": r"sk-\w+", "action": "redact"},
        {"name": "forbidden", "pattern": "launch-codes", "action": "block"},
    ])
    out, blocked = rules.apply("your key is sk-abc123 ok")
    assert blocked is None
    assert "sk-abc123" not in out and "«redacted:apikey»" in out
    _, blocked = rules.apply("the launch-codes are...")
    assert blocked == "forbidden"
    assert StreamRules(None).active is False


def test_ttsr_applies_in_provider_stream(monkeypatch):
    import aether_engine.audit as audit_module
    from aether_engine.providers.base import ProviderError
    from aether_engine.providers.litellm_provider import LiteLLMProvider

    events = []

    class SpyLogger:
        def log_event(self, name, payload):
            events.append(name)

    monkeypatch.setattr(audit_module, "AuditLogger", SpyLogger)

    def chunk(text, finish=None):
        delta = SimpleNamespace(content=text, reasoning_content=None, thinking=None, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])

    class Stream:
        def __init__(self, chunks):
            self._chunks = chunks

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for c in self._chunks:
                yield c

    async def fake_acompletion(**kwargs):
        return Stream([chunk("key: sk-live-999"), chunk(" done", finish="stop")])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    provider = LiteLLMProvider(
        api_key="<redacted>", model="gemini-3.5-flash", provider_name="google_gemini",
        ttsr_rules=[{"name": "apikey", "pattern": r"sk-\w+", "action": "redact"}],
    )
    async def collect():
        return [c async for c in provider.call_stream([{"role": "user", "content": "hi"}])]

    chunks = asyncio.run(collect())
    joined = "".join(c.text or "" for c in chunks)
    assert "sk-live-999" not in joined
    assert "«redacted:apikey»" in joined

    # A block rule stops the stream with a ProviderError.
    provider2 = LiteLLMProvider(
        api_key="<redacted>", model="gemini-3.5-flash", provider_name="google_gemini",
        ttsr_rules=[{"name": "apikey", "pattern": r"sk-\w+", "action": "block"}],
    )

    async def collect2():
        return [c async for c in provider2.call_stream([{"role": "user", "content": "hi"}])]

    with pytest.raises(ProviderError):
        asyncio.run(collect2())
    assert "TTSR_BLOCKED" in events


# ---------------------------------------------------------------------------
# E7-03 / E7-04 — skill disable, auto-match, catalogue
# ---------------------------------------------------------------------------


def _make_skill(global_dir: Path, name: str, body: str = "SECRET BODY", keywords=None, with_md=True):
    skill_dir = global_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "description": f"{name} description",
        "trigger": {
            "keywords": keywords or [],
            "slash_commands": [f"/{name}"],
            "auto_match": True,
        },
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    if with_md:
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description\n---\n\n{body}\n")
    return skill_dir


def test_disabled_skill_vanishes_from_agent_paths(tmp_path):
    from aether_engine.skills.manager import SkillManager

    gdir = tmp_path / "skills"
    _make_skill(gdir, "alpha", keywords=["deploy"])
    mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
    assert mgr.resolve_slash_command("/alpha") is not None
    assert "SECRET BODY" in mgr.load_skill_content("alpha")

    mgr.set_disabled("alpha", True)
    assert mgr.resolve_slash_command("/alpha") is None
    assert mgr.load_skill_content("alpha") == ""
    summaries = {s.name: s for s in mgr.list_all()}
    assert summaries["alpha"].enabled is False  # Settings can still see it

    # Persists across manager instances.
    mgr2 = SkillManager(global_dir=gdir, workspace_dirs=[])
    assert mgr2.resolve_slash_command("/alpha") is None

    mgr.set_disabled("alpha", False)
    assert mgr.resolve_slash_command("/alpha") is not None


def test_catalogue_has_summaries_not_bodies(tmp_path):
    from aether_engine.skills.manager import SkillManager

    gdir = tmp_path / "skills"
    _make_skill(gdir, "alpha", keywords=["deploy"])
    mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
    cat = mgr.catalogue()
    assert "alpha" in cat and "alpha description" in cat
    assert "SECRET BODY" not in cat


def test_auto_match_threshold(tmp_path):
    from aether_engine.skills.manager import SkillManager

    gdir = tmp_path / "skills"
    # Keywords live on manifest.json skills (a SKILL.md in the same
    # dir would shadow the manifest — the manager's documented
    # filename priority — and SKILL.md manifests carry no keywords).
    _make_skill(gdir, "beta", keywords=["deploy", "release"], with_md=False)
    mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
    assert [m.name for m in mgr.match_skills("deploy and release now", "supervisor")] == ["beta"]
    assert mgr.match_skills("unrelated words entirely", "supervisor") == []
    # A disabled keyword skill stops matching too.
    mgr.set_disabled("beta", True)
    assert mgr.match_skills("deploy and release now", "supervisor") == []


# ---------------------------------------------------------------------------
# E7-05 — plugin toggle teardown / rebuild (+ known-good acceptance)
# ---------------------------------------------------------------------------


def test_plugin_toggle_hides_and_restores(tmp_path):
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    from aether_engine.plugins.installer import PluginInstaller

    plugin_name = "TogglePlugin7"
    skill_name = "toggle-skill-7"
    server_name = "toggle-server-7"
    skills_root = Path.home() / ".aether" / "skills"
    try:
        source = tmp_path / "pkg"
        source.mkdir()
        manifest = {
            "name": plugin_name,
            "version": "1.0",
            "skills": [{"name": skill_name, "file_path": "skill.md"}],
            "mcp_servers": [{"name": server_name, "file_path": "server.json"}],
        }
        (source / "manifest.json").write_text(json.dumps(manifest))
        (source / "skill.md").write_text("toggle skill body")
        (source / "server.json").write_text(json.dumps({"command": "python3", "args": ["--version"]}))

        installer = PluginInstaller(tmp_path / "plugins")
        installer.install(installer.prepare_install(str(source)))

        # Known-good acceptance: skill on disk + server registered.
        assert (skills_root / skill_name / "SKILL.md").exists()
        assert server_name in GLOBAL_MCP_REGISTRY.list_servers()

        installer.toggle(plugin_name, False)
        assert server_name not in GLOBAL_MCP_REGISTRY.list_servers()
        assert not (skills_root / skill_name).exists()
        assert (skills_root / ".disabled-by-plugin" / plugin_name / skill_name / "SKILL.md").exists()

        installer.toggle(plugin_name, True)
        assert (skills_root / skill_name / "SKILL.md").exists()
        assert server_name in GLOBAL_MCP_REGISTRY.list_servers()
    finally:
        # Clean the shared home skills dir + registry entry.
        shutil.rmtree(skills_root / skill_name, ignore_errors=True)
        shutil.rmtree(skills_root / ".disabled-by-plugin" / plugin_name, ignore_errors=True)
        try:
            GLOBAL_MCP_REGISTRY.remove_server_sync(server_name)
        except Exception:  # noqa: BLE001, S110 — test cleanup: the server may already be gone
            pass
