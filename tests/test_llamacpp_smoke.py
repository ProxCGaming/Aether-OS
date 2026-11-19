"""Tool-call smoke suite for the llama.cpp router backend.

Stands up a fake llama.cpp router with the stdlib (http.server
in a thread) implementing the documented router-mode endpoints —
GET /health, GET /models, POST /models/load, POST
/models/unload, POST /v1/chat/completions — and proves, with no
real llama.cpp anywhere:

1. the manager reads status and the model list (with load
   states) from the fake router;
2. load / unload calls reach the router and flip its state;
3. one chat completion round trip flows through the engine's
   existing OpenAI-compatible provider path (LiteLLMProvider
   with api_base = the router URL — the construction the
   provider factory uses for OpenAI-compatible endpoints),
   carrying a tool definition, and the streamed tool_call
   response parses into the engine's ToolCall shape.

This is the "tool-call smoke suite" the hybrid local-serving
decision called for: if a future llama.cpp build changes its
wire shape, this suite is where the breakage shows first.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aether_engine.models.llama_router import LlamaRouterManager
from aether_engine.providers.base import ToolCall
from aether_engine.providers.litellm_provider import LiteLLMProvider

MODEL_ID = "local/qwen2.5-7b-q4.gguf"
OTHER_MODEL_ID = "local/nomic-embed-text.gguf"


class FakeLlamaRouter:
    """State + request log shared with the handler class."""

    def __init__(self):
        self.loaded = {MODEL_ID: True, OTHER_MODEL_ID: False}
        self.chat_requests: list[dict] = []
        self.load_requests: list[dict] = []
        self.unload_requests: list[dict] = []


def _make_handler(state: FakeLlamaRouter):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the suite quiet
            pass

        def _send_json(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                return {}

        def do_GET(self):
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
            elif self.path == "/models":
                self._send_json(200, {"data": [
                    {"id": model_id,
                     "status": {"value": "loaded" if loaded
                                else "unloaded"}}
                    for model_id, loaded in state.loaded.items()
                ]})
            else:
                self._send_json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if self.path == "/models/load":
                body = self._read_json()
                state.load_requests.append(body)
                if body.get("model") in state.loaded:
                    state.loaded[body["model"]] = True
                    self._send_json(200, {"success": True})
                else:
                    self._send_json(404, {"error": {
                        "message": "model not found",
                        "type": "invalid_request_error"}})
            elif self.path == "/models/unload":
                body = self._read_json()
                state.unload_requests.append(body)
                if body.get("model") in state.loaded:
                    state.loaded[body["model"]] = False
                    self._send_json(200, {"success": True})
                else:
                    self._send_json(404, {"error": {
                        "message": "model not found",
                        "type": "invalid_request_error"}})
            elif self.path == "/v1/chat/completions":
                self._chat_completion()
            else:
                self._send_json(404, {"error": {"message": "not found"}})

        def _chat_completion(self):
            body = self._read_json()
            state.chat_requests.append(body)
            # A streamed tool call, arguments split across two
            # deltas the way a real server emits them.
            chunks = [
                {"id": "chatcmpl-smoke", "object": "chat.completion.chunk",
                 "created": 0, "model": body.get("model", MODEL_ID),
                 "choices": [{"index": 0, "delta": {
                     "role": "assistant", "content": "",
                     "tool_calls": [{"index": 0, "id": "call_smoke_1",
                                     "type": "function",
                                     "function": {
                                         "name": "get_weather",
                                         "arguments": ""}}]},
                     "finish_reason": None}]},
                {"id": "chatcmpl-smoke", "object": "chat.completion.chunk",
                 "created": 0, "model": body.get("model", MODEL_ID),
                 "choices": [{"index": 0, "delta": {
                     "tool_calls": [{"index": 0, "function": {
                         "arguments": "{\"city\": \"Kol"}}]},
                     "finish_reason": None}]},
                {"id": "chatcmpl-smoke", "object": "chat.completion.chunk",
                 "created": 0, "model": body.get("model", MODEL_ID),
                 "choices": [{"index": 0, "delta": {
                     "tool_calls": [{"index": 0, "function": {
                         "arguments": "kata\"}"}}]},
                     "finish_reason": None}]},
                {"id": "chatcmpl-smoke", "object": "chat.completion.chunk",
                 "created": 0, "model": body.get("model", MODEL_ID),
                 "choices": [{"index": 0, "delta": {},
                              "finish_reason": "tool_calls"}]},
            ]
            payload = "".join(
                f"data: {json.dumps(c)}\n\n" for c in chunks)
            payload += "data: [DONE]\n\n"
            data = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


@pytest.fixture
def fake_router():
    state = FakeLlamaRouter()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield base_url, state
    server.shutdown()
    server.server_close()


async def test_manager_status_and_models(fake_router):
    base_url, _state = fake_router
    manager = LlamaRouterManager(router_url=base_url)

    status = await manager.router_status()
    assert status["running"] is True
    assert status["models_loaded"] == 1
    assert status["error"] is None

    listing = await manager.list_models()
    assert listing["error"] is None
    rows = {r["id"]: r for r in listing["models"]}
    assert rows[MODEL_ID]["loaded"] is True
    assert rows[MODEL_ID]["preset"] == "qwen-2-5-7b"
    assert rows[MODEL_ID]["tier"] == "daily"
    assert rows[OTHER_MODEL_ID]["loaded"] is False
    assert rows[OTHER_MODEL_ID]["tier"] == "resident"


async def test_manager_load_unload_round_trip(fake_router):
    base_url, state = fake_router
    manager = LlamaRouterManager(router_url=base_url)

    assert await manager.load_model(OTHER_MODEL_ID) == {
        "success": True, "error": None}
    assert state.load_requests == [{"model": OTHER_MODEL_ID}]
    assert state.loaded[OTHER_MODEL_ID] is True

    assert await manager.unload_model(OTHER_MODEL_ID) == {
        "success": True, "error": None}
    assert state.unload_requests == [{"model": OTHER_MODEL_ID}]
    assert state.loaded[OTHER_MODEL_ID] is False

    missing = await manager.load_model("local/no-such-model.gguf")
    assert missing == {"success": False, "error": "model not found"}


async def test_chat_tool_call_round_trip_through_provider_path(fake_router):
    base_url, state = fake_router
    # The existing OpenAI-compatible provider path, constructed
    # the way the provider factory builds an OpenAI-compatible
    # endpoint: LiteLLMProvider with api_base = the router's
    # /v1 base. No new chat path is built for the router.
    provider = LiteLLMProvider(
        api_key="smoke-test-key",
        model=MODEL_ID,
        provider_name="custom_openai",
        base_url=f"{base_url}/v1",
        record_usage=False,
    )
    tools = [{
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }]
    chunks = [
        chunk async for chunk in provider.call_stream(
            messages=[{"role": "user",
                       "content": "What is the weather in Kolkata?"}],
            tools=tools,
        )
    ]

    # The request that reached the router carried the tool
    # definition and the router model id.
    assert len(state.chat_requests) == 1
    sent = state.chat_requests[0]
    assert sent["model"] == MODEL_ID
    assert sent["tools"][0]["function"]["name"] == "get_weather"

    # The streamed tool call parsed into the engine's shape.
    tool_calls = [tc for chunk in chunks for tc in (chunk.tool_calls or [])]
    assert len(tool_calls) == 1
    call = tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.name == "get_weather"
    assert call.args == {"city": "Kolkata"}
    assert call.call_id == "call_smoke_1"
