# AETHER System Architecture, Subsystems, Future Roadmap & AI Prompt Guide

**AETHER** is a local, privacy-first desktop AI operating layer and intelligent assistant built for Windows. It features a hybrid multi-process architecture combining a PySide6 HUD desktop client with an asynchronous FastAPI engine over an authenticated loopback WebSocket.

---

## Table of Contents
1. [High-Level Architecture](#1-high-level-architecture)
2. [Deep Dive: Core Subsystems](#2-deep-dive-core-subsystems)
   - [A. Frontend Client (`aether_ui`)](#a-frontend-client-aether_ui)
   - [B. Backend Engine (`aether_engine`)](#b-backend-engine-aether_engine)
   - [C. Shared Contracts (`aether_common`)](#c-shared-contracts-aether_common)
   - [D. Process Sandboxing (`aether_worker`)](#d-process-sandboxing-aether_worker)
3. [Security Architecture & Secret Management](#3-security-architecture--secret-management)
4. [Tool Execution & Approval Matrix](#4-tool-execution--approval-matrix)
5. [Future Roadmap & Expansion Opportunities](#5-future-roadmap--expansion-opportunities)
6. [Master Context Prompt for ChatGPT / Claude](#6-master-context-prompt-for-chatgpt--claude)

---

## 1. High-Level Architecture

AETHER separates the user interface and the core execution engine into two decoupled processes communicating over a fast, authenticated WebSocket link.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                    AETHER DESKTOP                                      │
├────────────────────────────────────────────────────┬───────────────────────────────────┤
│                  FRONTEND (aether_ui)              │          BACKEND (aether_engine)  │
│                                                    │                                   │
│  ┌──────────────────────────────────────────────┐  │  ┌─────────────────────────────┐  │
│  │   Frameless Window & Geometry Persistence    │  │  │  FastAPI / Uvicorn Server   │  │
│  │   (QPainter Orb HUD + Stream Log)            │  │  │  (/ws/tasks Authenticated)  │  │
│  └──────────────────────┬───────────────────────┘  │  └──────────────┬──────────────┘  │
│                         │                          │                 │                 │
│  ┌──────────────────────▼───────────────────────┐  │  ┌──────────────▼──────────────┐  │
│  │   Provider Health & Latency Badge            │◄─┼──┤  ProviderHealthManager       │  │
│  │   (Live Healthy/Degraded/Offline/Standby)   │  │  │  (Cooldowns, Non-blocking)  │  │
│  └──────────────────────────────────────────────┘  │  └─────────────────────────────┘  │
│                                                    │                                   │
│  ┌──────────────────────────────────────────────┐  │  ┌─────────────────────────────┐  │
│  │   Slide-out Settings & Model Discovery       │◄─┼──┤  Intelligent Capability     │  │
│  │   (Cloud APIs, Ollama Pull/Delete, Cron)    │  │  │  Router & Fallback Pipeline │  │
│  └──────────────────────────────────────────────┘  │  └──────────────┬──────────────┘  │
│                                                    │                 │                 │
│  ┌──────────────────────────────────────────────┐  │  ┌──────────────▼──────────────┐  │
│  │   Interactive Tool Approval Drawer           │◄─┼──┤  Worker Job Objects (Win32) │  │
│  │   (Human-in-the-loop, Class Overrides)      │  │  │  (Memory/CPU Sandboxing)    │  │
│  └──────────────────────────────────────────────┘  │  └──────────────┬──────────────┘  │
│                                                    │                 │                 │
│                                                    │  ┌──────────────▼──────────────┐  │
│                                                    │  │  Windows DPAPI Secret Store │  │
│                                                    │  │  (~/.aether/secrets.db)     │  │
│                                                    │  └─────────────────────────────┘  │
└────────────────────────────────────────────────────┴───────────────────────────────────┘
```

---

## 2. Deep Dive: Core Subsystems

### A. Frontend Client (`aether_ui`)

| File / Component | Responsibility |
| :--- | :--- |
| `main.py` | Main frameless desktop shell with custom title bar controls, persistent window dimensions (`~/.aether/config.json`), and integration with `qasync` event loop. |
| `hud.py` | Primary HUD containing connection status badge, live provider health badge, model selector dropdown, prompt input bar, routing decision banner, and streaming event log. |
| `orb.py` | Native `QPainter` animated AI core that pulses and changes color states (`DISCONNECTED`, `CONNECTING`, `CONNECTED`, `TASK_RUNNING`, `ERROR`). |
| `models_panel.py` | Slide-out drawer managing Cloud providers (Gemini, OpenAI, Anthropic, Groq, Mistral, DeepSeek, OpenRouter), local Ollama model downloads/deletions, and capability check schedules. |
| `ws_client.py` | Asynchronous WebSocket client using `qasync` with automatic exponential backoff reconnection, token authentication injection, and event dispatching. |
| `state.py` | Strict UI State Machine (`UIState`: `DISCONNECTED`, `CONNECTING`, `CONNECTED`, `TASK_RUNNING`, `ERROR`). |
| `widgets/provider_health_badge.py` | Real-time health status pill showing latency (`● Healthy (145ms)`), failure alerts (`Degraded`, `Offline`), and diagnostic hover tooltips. |
| `widgets/routing_banner.py` | Transparency banner displaying model selection reasoning and fallback notices. |
| `components/approval_drawer.py` | Slide-in drawer for interactive human-in-the-loop tool approvals with risk class overrides. |

---

### B. Backend Engine (`aether_engine`)

| File / Component | Responsibility |
| :--- | :--- |
| `app.py` | FastAPI application serving `/ws/tasks` WebSocket endpoint, request routing, connection auditing, and provider health synchronization. |
| `orchestration/simple.py` | Multi-turn streaming task orchestrator handling LLM token streaming, on-demand tool execution, pre-flight safety validation, and safe fallback boundaries. |
| `langgraph/` | Hierarchical multi-agent workflow graph with `supervisor`, `researcher`, `planner`, and `coder` nodes backed by SQLite checkpointing. |
| `routing/health.py` | `ProviderHealthManager` and `ProviderHealthState` tracking runtime health, non-blocking latency, and failure cooldowns (15–60s for transient, 1hr for auth). |
| `routing/policy.py` & `capability_router.py` | Intelligent model router analyzing task requirements (coding, research, reasoning) to select optimal reachable models. |
| `routing/fallback.py` | Safe fallback classification (`classify_provider_error`) and candidate selection. Strictly enforces no fallbacks mid-tool execution. |
| `routing/startup.py` | Validates configuration, API keys, and model registry consistency on startup. |
| `providers/litellm_provider.py` | Unified LLM adapter wrapping LiteLLM for multi-provider streaming and tool calls. |
| `providers/discovery.py` | Automated live model discovery querying provider APIs. |
| `scheduler/capability_jobs.py` | Background capability check scheduler supporting cron and intervals with misfire recovery. |
| `audit/audit.py` | Immutable JSONL audit logging for security compliance and task transition tracking. |

---

### C. Shared Contracts (`aether_common`)

- **`contracts.py`**: Zero-dependency dataclass contracts (`Event`, `EventType`, `TaskState`). Enforces versioned schema parity (`SCHEMA_VERSION = "0.3.0"`).
- **`auth.py`**: Generation and cryptographic verification of temporary loopback authentication tokens (`~/.aether/auth_token`).

---

### D. Process Sandboxing (`aether_worker` & `workers/`)

- **`workers/job_object.py`**: Wraps risky tool executions in native Windows Win32 Job Objects (`CreateJobObjectW`, `SetInformationJobObject`) to enforce hard memory limits (e.g., 4096MB) and CPU time caps per task class.

---

## 3. Security Architecture & Secret Management

1. **Loopback WebSocket Security**:
   - Engine listens exclusively on loopback (`127.0.0.1:8765`).
   - Every connection must provide an authenticated Bearer token written to `~/.aether/auth_token` with restrictive OS permissions.
2. **Windows DPAPI Encryption**:
   - Provider API keys are encrypted at rest using Windows Crypt32 DPAPI (`CryptProtectData` / `CryptUnprotectData`) under `~/.aether/secrets.db`.
   - Keys cannot be decrypted by other user accounts on the machine.
3. **Workspace Boundary Containment**:
   - All filesystem operations are validated against configured workspace boundaries (`validate_workspace_containment`) to prevent directory traversal outside allowed folders.

---

## 4. Tool Execution & Approval Matrix

| Tool Name | Risk Classification | Human Approval Required? | Sandboxed in Worker Process? |
| :--- | :--- | :--- | :--- |
| `read_file` | Read-only | No | No (In-process) |
| `list_dir` | Read-only | No | No (In-process) |
| `get_current_time` | Read-only | No | No (In-process) |
| `write_file` | Standard / Mutating | Yes (Interactive Drawer) | Yes (Job Object) |
| `delete_file` | Critical / Mutating | Yes (Interactive Drawer) | Yes (Job Object) |
| `execute_shell` | Critical / Execution | Yes (Interactive Drawer) | Yes (Job Object) |

---

## 5. Future Roadmap & Expansion Opportunities

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 AETHER FUTURE ROADMAP                                  │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ Phase 1: Visual & Sensory Immersion                                                    │
│ ├── 60 FPS Gyroscopic Holographic Orb (3D Orbit Reticles & Reactive Particles)         │
│ ├── Low-Latency Voice I/O Loop (Local Whisper STT + Fast Neural TTS)                   │
│ └── Screen / Vision Context Snip Attachment                                            │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ Phase 2: Autonomous Intelligence & Knowledge                                           │
│ ├── Local Vector RAG (Chroma/LanceDB over local workspace files)                       │
│ ├── Multi-turn Agent Canvas & Interactive Artifact Viewer (HTML/Markdown/Diffs)        │
│ └── Model Context Protocol (MCP) Server Registry Expansion                             │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ Phase 3: Deep Desktop OS Integration                                                   │
│ ├── Global Spotlight Hotkey (e.g. Alt + Space summon/dismiss)                          │
│ ├── Autonomous Browser Automation Agent (Playwright integration)                       │
│ └── Windows Action Center Notification Integration                                     │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Advanced 3D Gyroscopic Orb**:
   - Upgrade `OrbWidget` to render multi-axis rotating reticles, particle star dust, and audio-reactive waveform rings with 60 FPS delta-time physics.
2. **Local Voice & Multimodal Vision**:
   - Real-time conversational voice pipeline using Whisper.cpp and Kokoro TTS.
   - Screen-region OCR and visual reasoning.
3. **Artifacts Canvas**:
   - Dedicated side-by-side workspace viewer for rendered code snippets, interactive diagrams, and live HTML/React previews.
4. **Local Workspace Knowledge (RAG)**:
   - Indexing user codebases and local notes into an embedded vector store for instant context retrieval.

---

## 6. Master Context Prompt for ChatGPT / Claude

Use the prompt below whenever starting a new conversation with ChatGPT or Claude to give the AI full context on the AETHER codebase:

```markdown
You are an expert AI system architect and Python engineer specializing in PySide6 desktop applications, FastAPI backend engines, and LLM orchestration systems.

I am developing an open-source local AI assistant called **AETHER** (repository: `Aether-OS`).

### Core Architecture & Tech Stack:
- **Architecture**: Hybrid multi-process client-server.
  - **Frontend (`aether_ui`)**: PySide6 desktop HUD running `qasync` to integrate Qt's event loop with Python asyncio.
  - **Backend Engine (`aether_engine`)**: FastAPI + Uvicorn service communicating over authenticated loopback WebSockets (`/ws/tasks`).
  - **Contracts (`aether_common`)**: Zero-dependency dataclass event contracts (`Event`, `EventType`, `TaskState`).
  - **Security**: Local WebSocket authentication via one-time tokens (`~/.aether/auth_token`) and Windows DPAPI encrypted secret storage (`~/.aether/secrets.db`).
  - **Sandboxing**: Windows Job Objects (`WorkerJobObject`) with memory/CPU time caps for risky tool executions.

### Directory Structure & Responsibilities:
```
Aether-OS/
├── aether_common/          # Shared contracts & token auth
│   ├── contracts.py        # Event schema, EventType, TaskState
│   └── auth.py             # Token generation & verification
├── aether_engine/          # FastAPI Engine
│   ├── app.py              # WebSocket endpoint, dispatch loop, health broadcast
│   ├── orchestration/      # Multi-turn streaming task runner & tool loop
│   ├── langgraph/          # Multi-agent graph (Supervisor, Researcher, Planner, Coder)
│   ├── routing/            # Capability matching, HealthManager, Fallback rules
│   ├── providers/          # LiteLLM adapter & dynamic model discovery
│   ├── secrets/            # Windows DPAPI encryption & SQLite storage
│   ├── tools/              # Tool registry & workspace file tools
│   ├── workers/            # Windows Job Object process containment
│   └── audit/              # JSONL task state transition logging
├── aether_ui/              # PySide6 Desktop Frontend
│   ├── main.py             # Frameless main window, geometry memory, drawer manager
│   ├── hud.py              # HUD view, provider health badge, model selector, log stream
│   ├── orb.py              # Native QPainter pulsing AI core
│   ├── models_panel.py     # Cloud & Local (Ollama) settings drawer
│   ├── ws_client.py        # Async WebSocket client with auto-reconnect
│   └── widgets/            # ProviderHealthBadge, RoutingBanner, ApprovalDrawer
├── run_engine.py           # Backend engine launcher
└── run_ui.py               # Desktop frontend launcher
```

### Key Engineering Rules:
1. **Never break contract parity**: Any event sent over `/ws/tasks` must match the schema in `aether_common/contracts.py`.
2. **Strict Tool Safety**: Tools modifying the filesystem or executing commands must pass workspace containment checks and request human approval via `TOOL_APPROVAL_REQUEST`.
3. **No Unsafe Mid-Loop Fallbacks**: Automatic provider fallback is allowed on initial prompt failure, but strictly forbidden once tools have started executing.
4. **Clean Concurrency**: Always use `asyncio.create_task` or Qt signals when bridging UI and WebSocket actions.

Please keep these architectural principles, directory paths, and design patterns in mind when helping me write code, debug issues, or design new features.
```
