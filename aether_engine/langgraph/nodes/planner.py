import logging

from langchain_core.runnables import RunnableConfig

from aether_engine.artifacts.manager import save_artifact
from aether_engine.langgraph.nodes.runner import SpecialistSpec, run_specialist
from aether_engine.langgraph.state import AetherState

logger = logging.getLogger("aether_engine.langgraph.planner")


def _planner_tail(content: str) -> dict:
    return {
        "plan": content,  # Overwrites existing plan
        "blackboard": {"plan": content},  # E1-15: share the plan on the blackboard
    }


PLANNER_SPEC = SpecialistSpec(
    name="planner",
    display_name="Planner",
    role_prompt="Break goals into structured steps, monitor progress, adapt the plan, or respond directly to conversational input.",
    fallback_prompt="Please proceed with breaking goals into structured steps and updating the plan.",
    proceed_prompt="Please proceed with breaking goals into structured steps and updating the plan based on the current state.",
    phase="planning",
    artifact_kind="plan",
    artifact_filename="planner_output.txt",
    artifact_key="planner_output",
    tail_extras=_planner_tail,
)


async def planner_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Planner node breaks goals into structured steps and monitors progress.
    Uses LiteLLMProvider from config for per-node fallback (ADR 0014 Fix 4).
    """
    # save_artifact is passed from this module's globals at call time so
    # the module-level patch seam (tests stub nodes.planner.save_artifact)
    # keeps working now that the shared body lives in runner.py.
    return await run_specialist(
        PLANNER_SPEC, state, config, save_artifact=save_artifact)
