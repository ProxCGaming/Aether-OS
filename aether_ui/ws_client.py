"""WebSocket client for connecting Aether UI to Aether Engine."""
import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable
from pathlib import Path

import websockets

from aether_common.auth import DEFAULT_TOKEN_PATH, read_token
from aether_common.contracts import Event, EventType
from aether_ui.state import UIStateMachine

logger = logging.getLogger("aether_ui.ws_client")


def _ws_header_kwarg(headers: dict) -> dict:
    """Return the correct keyword arg for websockets.connect across versions."""
    try:
        params = inspect.signature(websockets.connect).parameters
        if "additional_headers" in params:
            return {"additional_headers": headers}
        if "extra_headers" in params:
            return {"extra_headers": headers}
    except Exception:  # noqa: BLE001, S110 — legacy client: header probing is best-effort
        pass
    return {}


class AetherWSClient:
    def __init__(
        self,
        state_machine: UIStateMachine,
        server_uri: str = "ws://127.0.0.1:8000/ws/tasks",
        token_path: Path = DEFAULT_TOKEN_PATH,
        on_event: Callable[[Event], None] | None = None,
    ):
        self.sm = state_machine
        self.uri = server_uri
        self.token_path = Path(token_path)
        self.on_event = on_event
        self.ws = None
        self._running = False
        self._task: asyncio.Task | None = None

    async def connect_and_listen(self):
        self._running = True
        self.sm.set_connecting()

        while self._running:
            try:
                try:
                    token = read_token(self.token_path)
                except (FileNotFoundError, ValueError):
                    self.sm.set_connecting()
                    await asyncio.sleep(1.0)
                    continue

                # Header only: a token in the URL query string leaks
                # into logs and history. The engine accepts the
                # Authorization header, and the live Electron client
                # authenticates the same way (ADR 0024).
                kwargs = _ws_header_kwarg({"Authorization": f"Bearer {token}"})

                async with websockets.connect(self.uri, **kwargs) as ws:
                    self.ws = ws
                    logger.info("Connected to AETHER Engine.")
                    # Automatically request initial provider & model list upon connection
                    asyncio.create_task(self.request_provider_list())
                    while self._running:
                        raw = await ws.recv()
                        try:
                            ev = Event.from_json(raw)
                            self.sm.transition_on_event(ev)
                            if self.on_event:
                                self.on_event(ev)
                        except Exception as e:  # noqa: BLE001 — legacy client: a bad frame must not kill the receive loop
                            logger.error(f"Parse error: {e}")

            except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                logger.warning(f"Connection lost: {e}. Retrying…")
                self.sm.set_disconnected()
                self.ws = None
                if self._running:
                    await asyncio.sleep(1.5)
            except Exception as e:  # noqa: BLE001 — legacy client error boundary
                logger.error(f"WS client error: {e}")
                self.sm.set_error(str(e))
                self.ws = None
                if self._running:
                    await asyncio.sleep(2.0)

    async def _send(self, event: Event):
        if not self.ws:
            return
        self.sm.transition_on_event(event)
        if self.on_event:
            self.on_event(event)
        await self.ws.send(event.to_json())

    async def start_task(
        self,
        prompt: str = "Say hello",
        model: str = "gemini-2.0-flash",
        session_id: str | None = None,
        request_id: str | None = None,
    ):
        self.sm.clear_error()
        req_id = request_id or str(uuid.uuid4())
        payload = {"prompt": prompt, "model": model}
        if session_id:
            payload["session_id"] = session_id
            
        await self._send(
            Event(
                type=EventType.START_TASK,
                request_id=req_id,
                payload=payload,
            )
        )

    async def cancel_task(self, request_id: str | None = None):
        await self._send(Event(
            type=EventType.CANCEL_TASK,
            request_id=request_id or str(uuid.uuid4()),
        ))

    # Provider management & health helpers
    async def request_provider_list(self, request_id: str | None = None):
        await self._send(Event(
            type=EventType.PROVIDER_LIST_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
        ))

    async def validate_provider(
        self,
        provider: str,
        api_key: str,
        base_url: str | None = None,
        request_id: str | None = None,
    ):
        payload = {"provider": provider, "api_key": api_key}
        if base_url:
            payload["base_url"] = base_url
        await self._send(Event(
            type=EventType.PROVIDER_VALIDATE_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload=payload,
        ))

    async def save_provider(
        self, provider: str, api_key: str, is_default: bool, default_model: str,
        base_url: str | None = None, display_name: str | None = None, provider_type: str | None = None,
        models: list | None = None, request_id: str | None = None,
    ):
        payload = {
            "provider": provider,
            "api_key": api_key,
            "is_default": is_default,
            "default_model": default_model,
        }
        if base_url is not None:
            payload["base_url"] = base_url
        if display_name:
            payload["display_name"] = display_name
        if provider_type:
            payload["provider_type"] = provider_type
        if models is not None:
            payload["models"] = models
        await self._send(Event(
            type=EventType.PROVIDER_SAVE_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload=payload,
        ))

    async def remove_provider(self, provider: str, request_id: str | None = None):
        await self._send(Event(
            type=EventType.PROVIDER_REMOVE_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload={"provider": provider},
        ))

    async def reveal_provider_key(self, provider: str, request_id: str | None = None):
        await self._send(Event(
            type=EventType.PROVIDER_REVEAL_KEY_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload={"provider": provider},
        ))

    async def set_default_model(self, provider: str, model: str, request_id: str | None = None):
        await self._send(Event(
            type=EventType.MODEL_SET_DEFAULT,
            request_id=request_id or str(uuid.uuid4()),
            payload={"provider": provider, "model": model},
        ))

    # Local Models (Ollama) helpers
    async def request_local_models_list(self, request_id: str | None = None):
        await self._send(Event(
            type=EventType.LOCAL_MODEL_LIST_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
        ))

    async def start_local_model_download(self, model: str, destination_dir: str | None = None, request_id: str | None = None):
        await self._send(Event(
            type=EventType.LOCAL_MODEL_DOWNLOAD_START,
            request_id=request_id or str(uuid.uuid4()),
            payload={"model": model, "destination_dir": destination_dir},
        ))

    async def delete_local_model(self, model: str, request_id: str | None = None):
        await self._send(Event(
            type=EventType.LOCAL_MODEL_DELETE_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload={"model": model},
        ))

    # Capabilities Check Scheduler helpers
    async def set_capability_schedule(self, schedule: str, method: str, request_id: str | None = None):
        await self._send(Event(
            type=EventType.CAPABILITY_CHECK_SET_SCHEDULE,
            request_id=request_id or str(uuid.uuid4()),
            payload={"schedule": schedule, "method": method},
        ))

    async def run_capability_check_now(self, method: str | None = None, request_id: str | None = None):
        await self._send(Event(
            type=EventType.CAPABILITY_CHECK_RUN_NOW,
            request_id=request_id or str(uuid.uuid4()),
            payload={"method": method},
        ))

    async def refresh_models(
        self, 
        provider: str | None = None, 
        api_key: str | None = None, 
        base_url: str | None = None, 
        request_id: str | None = None
    ):
        payload = {"provider": provider} if provider else {}
        if api_key:
            payload["api_key"] = api_key
        if base_url:
            payload["base_url"] = base_url
            
        await self._send(Event(
            type=EventType.REFRESH_MODELS_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload=payload,
        ))

    async def request_capability_history(self, request_id: str | None = None):
        await self._send(Event(
            type=EventType.CAPABILITY_CHECK_GET_HISTORY,
            request_id=request_id or str(uuid.uuid4()),
        ))

    # Session System helpers
    async def fetch_sessions(self, request_id: str | None = None):
        await self._send(Event(
            type=EventType.SESSION_LIST_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
        ))

    async def get_session(self, session_id: str, request_id: str | None = None):
        await self._send(Event(
            type=EventType.SESSION_GET_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload={"session_id": session_id}
        ))

    async def create_session(self, title: str = "New Chat", request_id: str | None = None):
        await self._send(Event(
            type=EventType.SESSION_CREATE_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload={"title": title}
        ))

    async def delete_session(self, session_id: str, request_id: str | None = None):
        await self._send(Event(
            type=EventType.SESSION_DELETE_REQUEST,
            request_id=request_id or str(uuid.uuid4()),
            payload={"session_id": session_id}
        ))

    async def send_approval(self, granted: bool, approval_key: str | None = None, request_id: str | None = None, override_class: str | None = None):
        payload = {}
        if approval_key:
            payload["approval_key"] = approval_key
        if override_class:
            payload["override_class"] = override_class
        await self._send(Event(
            type=(EventType.TOOL_APPROVAL_GRANTED if granted else EventType.TOOL_APPROVAL_REJECTED),
            request_id=request_id or str(uuid.uuid4()),
            payload=payload,
        ))

    def start_background_listener(self):
        self._task = asyncio.ensure_future(self.connect_and_listen())
        return self._task

    async def disconnect(self):
        self._running = False
        if self.ws:
            await self.ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
        self.sm.set_disconnected()
