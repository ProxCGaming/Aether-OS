"""Per-connection context for the task websocket (Wave 5 split).

Every handler inside ``ws_tasks`` used to be a closure over the
endpoint's locals — the send-locked socket, the audit logger, the
background-task registry and the per-name lock maps. ``WsConnection``
gathers exactly those per-connection pieces into one object, so the
handler families can move out of the endpoint into their own
modules and be handed this context instead of closing over the
endpoint. It holds no engine state of its own: engine-wide state
stays on the ``engine_state`` singleton, and one context is created
per connection, exactly as the closure locals were.
"""
import asyncio
from typing import Any

from fastapi import WebSocket


class WsConnection:
    """One task-websocket connection's shared working state.

    ``ws`` is the endpoint's send-locked socket — the
    ``_SendLockedWebSocket`` proxy ``ws_tasks`` rebinds its socket
    reference to right after accept — so every send through it
    serialises through the one send lock (N-05).
    """

    def __init__(self, ws: WebSocket, audit_logger: Any) -> None:
        self.ws = ws
        self.audit_logger = audit_logger
        # N-19: every fire-and-forget helper spawned for this
        # connection registers here so disconnect cleanup can cancel
        # the work that would otherwise outlive its socket (sends
        # into a dead connection, downloads nobody is waiting for).
        self.background_tasks: set[asyncio.Task] = set()
        self._mcp_server_locks: dict[str, asyncio.Lock] = {}
        self._plugin_lifecycle_locks: dict[str, asyncio.Lock] = {}

    def spawn_background(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    def mcp_server_lock(self, name: str) -> asyncio.Lock:
        #
        # Race guard: the serial loop also implicitly guaranteed one MCP
        # operation per server at a time. The client layer is built for
        # cross-task session ownership (disconnects are routed through
        # each session's owner task), but two operations on the SAME
        # server — connect racing remove, a resource read racing
        # disconnect — could still interleave destructively against the
        # registry's session/config state, so each helper holds a
        # per-server lock around its registry work. Operations on
        # different servers, and every other message, stay parallel. The
        # map is per-connection (like active_downloads): separate
        # connections always had separate loops, so cross-connection
        # contention remains the registry/client layer's existing
        # contract, unchanged by this slice.
        lock = self._mcp_server_locks.get(name)
        if lock is None:
            lock = self._mcp_server_locks[name] = asyncio.Lock()
        return lock

    def plugin_lifecycle_lock(self, name: str) -> asyncio.Lock:
        # A per-plugin lock preserves the serial loop's ordering
        # between lifecycle operations on the same plugin, as the
        # MCP helpers' per-server lock does (RUX-B-04).
        lock = self._plugin_lifecycle_locks.get(name)
        if lock is None:
            lock = self._plugin_lifecycle_locks[name] = asyncio.Lock()
        return lock
