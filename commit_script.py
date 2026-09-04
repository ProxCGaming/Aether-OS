import os
import subprocess

def run(cmd):
    print(f"Running: {cmd}")
    subprocess.run(cmd, shell=True, check=True)

commits = [
    {
        "files": [
            "aether_engine/orchestration/simple.py",
            "tests/test_orchestration_simple.py",
            "aether_engine/providers/gemini.py"
        ],
        "msg": "Remove deprecated simple orchestrator and legacy gemini provider"
    },
    {
        "files": [
            "aether_engine/langgraph/checkpointer.py",
            "aether_engine/langgraph/executor.py",
            "aether_engine/langgraph/graph.py",
            "aether_engine/langgraph/state.py",
            "aether_engine/langgraph/nodes/coder.py",
            "aether_engine/langgraph/nodes/execute_tool.py",
            "aether_engine/langgraph/nodes/pause.py",
            "aether_engine/langgraph/nodes/planner.py",
            "aether_engine/langgraph/nodes/researcher.py",
            "aether_engine/langgraph/nodes/supervisor.py",
            "tests/test_langgraph_engine.py"
        ],
        "msg": "Refactor LangGraph orchestration: add nodes and improve checkpointer/state handling"
    },
    {
        "files": [
            "aether_engine/app.py",
            "tests/test_engine_ws.py"
        ],
        "msg": "Update core engine app and websocket endpoints for LangGraph integration"
    },
    {
        "files": [
            "aether_engine/providers/litellm_provider.py",
            "aether_engine/providers/discovery.py"
        ],
        "msg": "Enhance LiteLLM provider integration and model discovery"
    },
    {
        "files": [
            "aether_engine/workers/sandbox.py",
            "aether_engine/validation/pre_flight.py",
            "aether_engine/validation/__init__.py",
            "tests/test_pre_flight_validation.py",
            "tests/test_phase5_1_fixes.py"
        ],
        "msg": "Implement sandbox worker execution and pre-flight validation logic"
    },
    {
        "files": [
            "aether_ui/components/approval_drawer.py",
            "aether_ui/hud.py",
            "aether_ui/main.py",
            "aether_ui/orb.py",
            "aether_ui/config_window.py",
            "aether_ui/settings_window.py",
            "aether_ui/theme.py",
            "aether_ui/widgets/provider_health_badge.py",
            "aether_ui/widgets/routing_banner.py",
            "tests/test_provider_health_badge.py",
            "tests/test_ui_window_geometry.py"
        ],
        "msg": "Update UI components: add approval drawer, health badges, and theme refinements"
    },
    {
        "files": [
            "aether_engine/mcp/fallback.py",
            "aether_engine/mcp/registry.py",
            "tests/test_fallback_rules.py",
            "tests/test_mcp_artifacts.py"
        ],
        "msg": "Improve MCP fallback rules and tool registry handling"
    },
    {
        "files": [
            "aether_engine/routing/health.py",
            "aether_engine/secrets/dpapi.py",
            "scripts/export_keys.py",
            "tests/test_tool_registry.py"
        ],
        "msg": "Fix DPAPI null-terminator issues and enhance routing health checks"
    },
    {
        "files": [
            "ADR/0009-execution-sandbox-and-hitl.md",
            "ADR/0011-langgraph-orchestration-and-native-approval.md",
            "ADR/0012-dpapi-null-terminator-fix.md",
            "ADR/0014-phase-5-1-sandbox-and-durability-fixes.md",
            "docs/AETHER_SYSTEM_GUIDE.md",
            "docs/AETHER_Phase5.1_Agent_Prompt.md",
            "docs/adr/0009-execution-sandbox-and-hitl.md",
            "docs/adr/README.md",
            "docs/implementation-notes/phase4-sandbox-hitl.md",
            ".agents/AGENTS.md"
        ],
        "msg": "Update documentation and ADRs for Phase 5.1, Sandbox, HitL, and LangGraph architecture"
    },
    {
        "files": [
            "aether_engine/artifacts/manager.py",
            "aether_worker/__main__.py"
        ],
        "msg": "Update artifact manager and worker entrypoint"
    }
]

def git_commit_files():
    for commit in commits:
        for f in commit["files"]:
            try:
                run(f'git add "{f}"')
            except Exception as e:
                print(f"Failed to add {f}")
        
        try:
            run(f'git commit -m "{commit["msg"]}"')
        except Exception as e:
            print(f"Failed to commit: {commit['msg']}")

if __name__ == "__main__":
    git_commit_files()
    
    # Catch any remaining untracked or modified files
    run("git add .")
    try:
        run('git commit -m "chore: formatting and miscellaneous updates"')
    except:
        pass
    
    # Push to origin main
    run("git push origin main")
