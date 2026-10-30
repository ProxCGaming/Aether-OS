"""Epic 1, E1-14: config layers — global → project → task overlay.

A project config (.aether/config.json in the workspace) overrides the
global config for tasks in that workspace only; a per-task overlay
wins over both. Dicts merge key-wise, arrays replace. The project
layer may not touch the user's safety settings (tool_policies,
workspace_roots). With no project file and no overlay, the global
config object is returned unchanged.
"""
import json

from aether_engine.config import UserConfig
from aether_engine.config_layers import (
    effective_user_config,
    load_project_layer,
    merge_layers,
)


def test_merge_semantics_dicts_merge_arrays_replace():
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "list": [1, 2], "keep": "yes"}
    over = {"a": 2, "nested": {"y": 3}, "list": [9], "keep": None}
    merged = merge_layers(base, over)
    assert merged == {"a": 2, "nested": {"x": 1, "y": 3}, "list": [9], "keep": "yes"}


def _write_project(tmp_path, data):
    cfg_dir = tmp_path / ".aether"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return str(tmp_path)


def test_load_project_layer_tolerates_missing_and_broken_files(tmp_path):
    assert load_project_layer(str(tmp_path)) == {}
    assert load_project_layer(None) == {}
    cfg_dir = tmp_path / ".aether"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{not json", encoding="utf-8")
    assert load_project_layer(str(tmp_path)) == {}
    (cfg_dir / "config.json").write_text('["a", "list"]', encoding="utf-8")
    assert load_project_layer(str(tmp_path)) == {}


def test_project_layer_overrides_global_for_its_workspace_only(tmp_path):
    global_cfg = UserConfig(agent_models={"coder": "global:strong"}, compaction_trigger_messages=40)
    ws = _write_project(tmp_path, {"agent_models": {"coder": "local:fast"},
                                   "compaction_trigger_messages": 10})

    effective = effective_user_config(global_cfg, workspace_root=ws)
    assert effective.agent_models == {"coder": "local:fast"}  # nested merge, project wins
    assert effective.compaction_trigger_messages == 10

    elsewhere = effective_user_config(global_cfg, workspace_root=str(tmp_path / "nope"))
    assert elsewhere.agent_models == {"coder": "global:strong"}
    assert global_cfg.agent_models == {"coder": "global:strong"}  # global never mutated


def test_no_layers_returns_the_global_object_itself(tmp_path):
    global_cfg = UserConfig()
    assert effective_user_config(global_cfg, workspace_root=str(tmp_path)) is global_cfg
    assert effective_user_config(global_cfg) is global_cfg


def test_task_overlay_beats_project_and_global(tmp_path):
    global_cfg = UserConfig(supervisor_delegation_cap=8)
    ws = _write_project(tmp_path, {"supervisor_delegation_cap": 3})
    effective = effective_user_config(global_cfg, workspace_root=ws,
                                      overlay={"supervisor_delegation_cap": 1})
    assert effective.supervisor_delegation_cap == 1
    assert effective_user_config(global_cfg, workspace_root=ws).supervisor_delegation_cap == 3


def test_project_layer_cannot_touch_safety_settings(tmp_path):
    global_cfg = UserConfig(tool_policies={"shell": "Deny"}, workspace_roots=["/safe"])
    ws = _write_project(tmp_path, {"tool_policies": {"shell": "Allow"},
                                   "workspace_roots": ["/tmp/evil"],
                                   "reasoning_effort": "high"})
    effective = effective_user_config(global_cfg, workspace_root=ws)
    assert effective.tool_policies == {"shell": "Deny"}      # untouched
    assert effective.workspace_roots == ["/safe"]            # untouched
    assert effective.reasoning_effort == "high"              # ordinary keys still layer
