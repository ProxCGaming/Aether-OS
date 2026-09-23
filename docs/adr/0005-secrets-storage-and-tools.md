# ADR 0005: Secrets Storage via Windows DPAPI and On-Demand Tool Calling

## Status
Accepted

## Context
Phase 2 introduces real LLM provider calls (Google Gemini) and tool execution into AETHER.
1. **API Keys & Secrets Storage**: Storing plaintext API keys in configuration files, environment variables, or within the repository risks accidental leakage or unauthorized access by local untrusted applications.
2. **Tool Calling & Context Management**: Bulk pre-injecting dynamic system state (such as the current time, hardware statistics, or files) into every LLM prompt bloats token usage and degrades model reasoning. Tools must be declared explicitly and executed on-demand only when the model requests them.

## Decision
1. **Local Secrets Storage with Windows DPAPI**:
   - Secrets are stored in a local SQLite database at `~/.aether/secrets.db` (strictly outside the repository directory).
   - API keys are encrypted at rest using Windows Data Protection API (`CryptProtectData` / `CryptUnprotectData`) via Python's standard library `ctypes`.
   - No third-party crypto packages are required (`pywin32` or `cryptography` are avoided for the secrets layer).
   - Decryption failures (e.g., cross-account database copies or corrupted ciphertext) raise a specific `SecretDecryptionError`.
   - API keys are read from `SecretStore` once at task start and held for that task's lifecycle.
2. **On-Demand Tool Registry**:
   - The LLM is provided tool definitions (name, description, parameter schemas) upfront.
   - Tools are only executed when the LLM emits a function call request (`function_call`).
   - Tool outputs are fed back to the LLM via an iterative turn loop in `run_task`.
   - Phase 2 implements a non-destructive `get_current_time` tool to validate the end-to-end multi-turn loop.

## Consequences
### Positive
- API keys are protected at the Windows OS credential level tied to the active user profile.
- Token consumption is minimal and deterministic because tool data is only generated when requested.
- Clean separation between provider interfaces, tool execution, and orchestration.

### Negative / Trade-offs
- Secrets database cannot be shared or decrypted across different Windows user accounts (by DPAPI design).
- Multi-turn tool loops require streaming intermediate status events (`TASK_PROGRESS`) back to the UI shell.
