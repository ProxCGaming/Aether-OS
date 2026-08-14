# ADR 0013: Dynamic Custom Provider Registry

## Status
Accepted (Supersedes ADR 0006 constraint on "Static provider registry")

## Context
ADR 0006 established that cloud providers are defined exclusively in a hardcoded static registry (`CLOUD_PROVIDERS` in `aether_engine/config.py`). This ensured a deterministic, offline-safe list of providers but introduced the explicit trade-off that adding new providers required a code change.

As the user workflow evolved to include self-hosted setups (e.g., Ollama, LM Studio) and varying cloud SDK endpoints, the restriction of exactly one "Custom OpenAI Compatible" slot became a severe bottleneck. Users need the ability to add *multiple* custom providers (e.g., both an Anthropic SDK endpoint and an OpenAI compatible local server), name them distinctively (e.g., "My Local LM Studio", "Work Ollama"), and have them persist as first-class citizens in the provider dropdown alongside the static cloud providers.

## Decision
1. **Hybrid Provider Registry**: We are shifting from a purely static registry to a hybrid model. 
   - Core cloud providers (Google Gemini, OpenAI, etc.) remain statically defined in `CLOUD_PROVIDERS` for out-of-the-box reliability.
   - User-defined custom providers can be added dynamically at runtime via the existing `PROVIDER_SAVE_REQUEST` event.

2. **Persistence Strategy**:
   - The engine's `UserConfig` (`~/.aether/config.json`) is extended with two new dicts: `custom_provider_names` (mapping provider keys to human-readable names) and `custom_provider_types` (identifying the integration flavor, e.g., `openai_compatible` or `anthropic`).
   - `_build_provider_list` and `_build_provider_list_async` in `aether_engine/app.py` are modified to append dynamically discovered custom providers (those found in config/secrets but not in `CLOUD_PROVIDERS`) to the `PROVIDER_LIST_RESPONSE` payload.

3. **UI Integration**:
   - The UI introduces a "Quick-Add Bar" allowing users to spawn arbitrary numbers of Custom OpenAI Compatible or Anthropic SDK providers.
   - Keys are auto-generated from the user's custom name (e.g., `my_local_lm_studio`).
   - No structural changes to the core `save_provider` IPC loop are required; the UI simply transmits the `display_name` and `provider_type` alongside the new key.

## Consequences
### Positive
- Users can run arbitrarily complex combinations of local and custom cloud endpoints.
- No schema breaking changes to `config.json`; older clients just ignore the new dynamic provider fields.
- Solves the single-slot limitation of the legacy "Custom" provider card.

### Negative / Trade-offs
- The provider list sent to the UI is now mutable in length and composition, requiring the frontend dropdown to cleanly handle dynamic rebuilding (which it already does via `cmb_provider.clear()`).
- Requires strict validation on custom provider key generation to avoid colliding with statically defined keys (e.g., a user cannot name their provider "Google Gemini").
