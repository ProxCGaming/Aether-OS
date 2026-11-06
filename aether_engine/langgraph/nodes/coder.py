import logging

from langchain_core.runnables import RunnableConfig

from aether_engine.artifacts.manager import save_artifact
from aether_engine.langgraph.nodes.runner import SpecialistSpec, run_specialist
from aether_engine.langgraph.state import AetherState

logger = logging.getLogger("aether_engine.langgraph.coder")

CODER_SPEC = SpecialistSpec(
    name="coder",
    display_name="Coder",
    role_prompt="Write, test, and debug code based on the current plan and context.",
    fallback_prompt="Please proceed with writing, testing, or debugging the code based on the current state.",
    proceed_prompt="Please proceed with writing, testing, or debugging the code based on the current state.",
    phase="coding",
    artifact_kind="code",
    artifact_filename="coder_output.txt",
    artifact_key="coder_output",
    tail_extras=None,
)


async def coder_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Coder node writes, tests, and debugs code.
    Uses LiteLLMProvider from config for per-node fallback (ADR 0014 Fix 4).
    """
    # save_artifact is passed from this module's globals at call time so
    # the module-level patch seam (tests stub nodes.coder.save_artifact)
    # keeps working now that the shared body lives in runner.py.
    return await run_specialist(
        CODER_SPEC, state, config, save_artifact=save_artifact)
