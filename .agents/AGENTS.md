# AETHER v2 Context & Architecture

This is a local AI assistant application named "AETHER".

## Architecture
The application uses a hybrid multi-process architecture:
- **Frontend (`aether_ui`)**: A desktop UI built with `PySide6` and native QPainter (Orb HUD) + `qasync`.
- **Backend (`aether_engine`)**: A local `FastAPI` / `uvicorn` engine with Google Gemini & tool calling.
- **Contracts (`aether_common`)**: Shared, zero-dependency dataclass event contracts.
- **Communication**: The frontend communicates with the backend via authenticated WebSockets (`/ws/tasks`).

## File Structure & Responsibilities
- `aether_ui/`: PySide6 desktop shell.
  - `main.py`: Entry point for UI. Launches `AetherWindow` using `qasync`.
  - `hud.py`: Main window, glowing Orb, input box, model selector dropdown, task streaming view.
  - `state.py`: State machine for connection, task lifecycle, and error handling.
  - `ws_client.py`: WebSocket client connecting to the Engine.
- `aether_engine/`: FastAPI backend.
  - `app.py`: FastAPI server, WebSocket endpoint (`/ws/tasks`), audit logger.
  - `secrets/`: DPAPI encrypted storage (`~/.aether/secrets.db`).
  - `providers/`: LLM provider adapters (Google Gemini).
  - `orchestration/`: Multi-turn streaming task orchestrator.
  - `tools/`: On-demand tool registry.
- `aether_common/`: Dataclass contracts and authentication helpers.
- `run_ui.py`: Launcher script for Aether UI.
- `run_engine.py`: Launcher script for Aether Engine.

## Security Mechanism
The local WebSocket connection requires authentication via standard tokens. Secrets are encrypted using Windows DPAPI under `~/.aether/secrets.db`.
