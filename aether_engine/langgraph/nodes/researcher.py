import logging

from langchain_core.runnables import RunnableConfig

from aether_engine.artifacts.manager import save_artifact
from aether_engine.langgraph.nodes.runner import SpecialistSpec, run_specialist
from aether_engine.langgraph.state import AetherState

logger = logging.getLogger("aether_engine.langgraph.researcher")


def _researcher_tail(content: str) -> dict:
    finding = " ".join((content or "").split())[:200]
    return {
        "blackboard": {"findings": [finding]} if finding else {},  # E1-15: share the finding
    }


RESEARCHER_SPEC = SpecialistSpec(
    name="researcher",
    display_name="Researcher",
    role_prompt="Gather information and synthesize findings for other specialists.",
    fallback_prompt="Please proceed with gathering information and synthesizing findings based on the current state.",
    proceed_prompt="Please proceed with gathering information and synthesizing findings based on the current state.",
    phase="researching",
    artifact_kind="research",
    artifact_filename="researcher_output.txt",
    artifact_key="researcher_output",
    tail_extras=_researcher_tail,
)


async def researcher_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Researcher node gathers info and updates the state.
    Uses LiteLLMProvider from config for per-node fallback (ADR 0014 Fix 4).
    """
    # save_artifact is passed from this module's globals at call time so
    # the module-level patch seam (tests stub nodes.researcher.save_artifact)
    # keeps working now that the shared body lives in runner.py.
    return await run_specialist(
        RESEARCHER_SPEC, state, config, save_artifact=save_artifact)
