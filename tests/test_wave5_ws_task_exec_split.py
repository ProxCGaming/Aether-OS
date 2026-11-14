"""Wave 5 (blow-up overhaul) — the websocket split, slice 3.

§1 of the seam map, final slice: the task-exec core
(``execute_task_generator`` + the START_TASK launch,
RUX-B-07) leaves ``ws_tasks`` for ``routes/ws_task_exec.py``,
and the receive loop's 123-branch if/elif chain becomes the
``_DISPATCH`` table over module-level handlers. The
per-connection task state the closures shared with the loop —
runner, session, launch tail, downloads, current task id,
prepared plugin installs — lives on ``WsConnection`` now.

These tests pin the shape of that move: the moved core is
importable from its new module (and re-imported by the
endpoint by identity), the pump's call signature is unchanged
after the leading ``conn`` (the voice family drives it through
the conn slot with exactly that shape), the connection's task
state starts empty and is per-connection, every event type the
old chain handled resolves in the table (and STEER_TASK does
not — the loop's pre-pass owns it), the three membership pairs
share their one handler, and the per-type dispositions are the
branches' own: START_TASK chains a launch task instead of
awaiting, VERSION_CHECK spawns onto the background registry,
PING answers inline, CANCEL_TASK reads the (empty) conn state
and answers honestly. Behaviour over the socket is the family
suites' contract, as in slices 1–2.
"""
import asyncio
import inspect
import json

from aether_common.contracts import Event, EventType
from aether_engine.routes import websocket, ws_connection, ws_task_exec

# Every event type the pre-slice if/elif chain dispatched on
# (123 branches; three are membership tests over a pair, hence
# 126 types). STEER_TASK is deliberately absent: the receive
# loop's pre-pass handles it before the table is consulted.
DISPATCHED_TYPES = (
    "AGENT_LIST_REQUEST",
    "AGENT_UPDATE_REQUEST",
    "APPROVAL_GRANTS_REQUEST",
    "APPROVAL_GRANT_REVOKE_REQUEST",
    "APPROVAL_MODE_SET_REQUEST",
    "ARTIFACT_GET_REQUEST",
    "ARTIFACT_LIST_REQUEST",
    "AUXILIARY_MODEL_SET_REQUEST",
    "BACKUP_CREATE_REQUEST",
    "BACKUP_RESTORE_REQUEST",
    "BROWSER_POLICY_GET_REQUEST",
    "BROWSER_POLICY_SET_REQUEST",
    "BROWSER_TASK_PAUSE_REQUEST",
    "CANCEL_TASK",
    "CAPABILITY_CHECK_GET_HISTORY",
    "CAPABILITY_CHECK_RUN_NOW",
    "CAPABILITY_CHECK_SET_SCHEDULE",
    "CONFIG_GET_REQUEST",
    "CONFIG_RESET_REQUEST",
    "CONFIG_UPDATE_REQUEST",
    "CONTEXT_BREAKDOWN_REQUEST",
    "DAILY_BUDGET_SET_REQUEST",
    "DEV_SERVER_DETECT_REQUEST",
    "DIAGNOSTICS_EXPORT_REQUEST",
    "DICTATION_CHUNK_REQUEST",
    "DICTATION_START_REQUEST",
    "DICTATION_STOP_REQUEST",
    "ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST",
    "LOCAL_MODEL_DELETE_REQUEST",
    "LOCAL_MODEL_DOWNLOAD_CANCEL",
    "LOCAL_MODEL_DOWNLOAD_START",
    "LOCAL_MODEL_LIST_REQUEST",
    "MACHINE_CAPABILITY_REQUEST",
    "MCP_PROMPT_LIST_REQUEST",
    "MCP_PROMPT_RUN_REQUEST",
    "MCP_RESOURCE_LIST_REQUEST",
    "MCP_RESOURCE_READ_REQUEST",
    "MCP_SERVER_ADD_REQUEST",
    "MCP_SERVER_CONNECT_REQUEST",
    "MCP_SERVER_DISCONNECT_REQUEST",
    "MCP_SERVER_LIST_REQUEST",
    "MCP_SERVER_REMOVE_REQUEST",
    "MCP_SERVER_TEST_REQUEST",
    "MCP_SERVER_UPDATE_REQUEST",
    "MEDIA_DELETE_REQUEST",
    "MEDIA_LIST_REQUEST",
    "MEDIA_METADATA_UPDATE_REQUEST",
    "MEDIA_SEARCH_REQUEST",
    "CALENDAR_LIST_REQUEST",
    "MEMORY_BACKENDS_REQUEST",
    "MEMORY_ENTITY_DELETE_REQUEST",
    "MEMORY_EPISODES_CLEAR_REQUEST",
    "MEMORY_EPISODES_REQUEST",
    "MEMORY_EPISODE_DELETE_REQUEST",
    "MEMORY_EXPORT_REQUEST",
    "MEMORY_FACT_DELETE_REQUEST",
    "MEMORY_FACT_UPDATE_REQUEST",
    "MEMORY_GRAPH_CLEAR_REQUEST",
    "MEMORY_GRAPH_REQUEST",
    "MODEL_HUB_DELETE_REQUEST",
    "MODEL_HUB_DETAIL_REQUEST",
    "MODEL_HUB_DOWNLOADED_REQUEST",
    "MODEL_HUB_DOWNLOAD_REQUEST",
    "MODEL_HUB_SEARCH_REQUEST",
    "MODEL_SET_DEFAULT",
    "PING",
    "PLUGIN_APPROVAL_GRANTED",
    "PLUGIN_APPROVAL_REJECTED",
    "PLUGIN_INSTALL_REQUEST",
    "PLUGIN_LIST_REQUEST",
    "PLUGIN_TOGGLE_REQUEST",
    "PLUGIN_UNINSTALL_REQUEST",
    "PROJECT_CONFIG_GET_REQUEST",
    "PROJECT_CONFIG_UPDATE_REQUEST",
    "PROVIDER_LIST_REQUEST",
    "PROVIDER_REMOVE_REQUEST",
    "PROVIDER_REVEAL_KEY_REQUEST",
    "PROVIDER_SAVE_REQUEST",
    "PROVIDER_VALIDATE_REQUEST",
    "READ_ALOUD_START_REQUEST",
    "READ_ALOUD_STOP_REQUEST",
    "REASONING_EFFORT_SET_REQUEST",
    "REFRESH_MODELS_REQUEST",
    "REWIND_MANIFEST_GET_REQUEST",
    "SESSION_CREATE_REQUEST",
    "SESSION_DELETE_REQUEST",
    "SESSION_EXPORT_REQUEST",
    "SESSION_FORK_REQUEST",
    "SESSION_GET_REQUEST",
    "SESSION_IMPORT_REQUEST",
    "SESSION_LIST_REQUEST",
    "SESSION_MOVE_REQUEST",
    "SESSION_RENAME_REQUEST",
    "SESSION_SEARCH_REQUEST",
    "SESSION_TRUNCATE_REQUEST",
    "SETTINGS_PROVIDER_VALIDATE_REQUEST",
    "SKILL_LIST_REQUEST",
    "SKILL_TOGGLE_REQUEST",
    "SLASH_COMMAND_LIST_REQUEST",
    "START_TASK",
    "TASK_LEDGER_LIST_REQUEST",
    "TASK_LEDGER_QUERY_REQUEST",
    "TASK_REWIND_REQUEST",
    "TASK_REWIND_UNDO_REQUEST",
    "TOOL_APPROVAL_GRANTED",
    "TOOL_APPROVAL_REJECTED",
    "TOOL_LIST_REQUEST",
    "TOOL_POLICY_SET_REQUEST",
    "USAGE_LEDGER_QUERY_REQUEST",
    "VAULT_ENTRY_DELETE_REQUEST",
    "VAULT_ENTRY_LIST_REQUEST",
    "VAULT_ENTRY_SAVE_REQUEST",
    "VAULT_FILL_APPROVAL_RESPONSE",
    "VAULT_TOTP_CODES_REQUEST",
    "VAULT_TOTP_DELETE_REQUEST",
    "VAULT_TOTP_LIST_REQUEST",
    "VAULT_TOTP_SAVE_REQUEST",
    "VERSION_CHECK_REQUEST",
    "VOICE_PLAYBACK_PROGRESS",
    "VOICE_RTC_ICE_REQUEST",
    "VOICE_RTC_OFFER_REQUEST",
    "VOICE_SESSION_START_REQUEST",
    "VOICE_SESSION_STOP_REQUEST",
    "WORKSPACE_ADD_REQUEST",
    "WORKSPACE_INDEX_CONFIG_REQUEST",
    "WORKSPACE_INDEX_PAUSE_REQUEST",
    "WORKSPACE_INDEX_START_REQUEST",
    "WORKSPACE_INDEX_STATUS_REQUEST",
    "WORKSPACE_LIST_REQUEST",
    "WORKSPACE_REMOVE_REQUEST",
    "WORKSPACE_SET_ACTIVE_REQUEST"
)


def _nested_code_names(code):
    names = set()
    for const in code.co_consts:
        if inspect.iscode(const):
            names.add(const.co_name)
            names |= _nested_code_names(const)
    return names


class TestMovedCore:
    def test_pump_and_launch_are_module_level(self):
        assert inspect.iscoroutinefunction(ws_task_exec.execute_task_generator)
        assert inspect.iscoroutinefunction(ws_task_exec.start_task_message)
        assert inspect.iscoroutinefunction(
            ws_task_exec._learn_fast_chat_in_background)
        assert inspect.iscoroutinefunction(
            ws_task_exec._title_session_in_background)

    def test_endpoint_reimports_the_moved_core_by_identity(self):
        assert websocket.execute_task_generator is \
            ws_task_exec.execute_task_generator
        assert websocket.start_task_message is ws_task_exec.start_task_message

    def test_ws_tasks_no_longer_defines_the_core_as_closures(self):
        nested = _nested_code_names(websocket.ws_tasks.__code__)
        assert "execute_task_generator" not in nested
        assert "_start_task_message" not in nested
        assert "_learn_fast_chat_in_background" not in nested
        assert "_title_session_in_background" not in nested

    def test_pump_signature_after_conn_matches_the_closure(self):
        # ws_voice calls the pump through the conn slot with this
        # exact shape (positional session_id, keyword rest).
        params = list(inspect.signature(
            ws_task_exec.execute_task_generator).parameters.values())
        assert params[0].name == "conn"
        rest = [(p.name, p.default) for p in params[1:]]
        assert rest == [
            ("generator", inspect.Parameter.empty),
            ("p_name", inspect.Parameter.empty),
            ("p_model", inspect.Parameter.empty),
            ("req_id", inspect.Parameter.empty),
            ("session_id", None),
            ("routing_reason", "Default Routing"),
            ("routing_capabilities", None),
            ("retry_factory", None),
            ("fallback_blocked_message", None),
        ]

    def test_launch_signature(self):
        params = list(inspect.signature(
            ws_task_exec.start_task_message).parameters)
        assert params == ["conn", "msg", "req_id", "previous_start"]


class TestWsConnectionTaskState:
    def test_task_state_defaults(self):
        conn = ws_connection.WsConnection(object(), object(), object())
        assert conn.task_runner is None
        assert conn.task_session_id is None
        assert conn.task_start_tail is None
        assert conn.active_downloads == {}
        assert conn.active_task_id is None
        assert conn.own_plugin_install_ids == set()
        # The pump slot still starts empty; ws_tasks binds it.
        assert conn.execute_task_generator is None

    def test_task_state_is_per_connection(self):
        a = ws_connection.WsConnection(object(), object(), object())
        b = ws_connection.WsConnection(object(), object(), object())
        assert a.active_downloads is not b.active_downloads
        assert a.own_plugin_install_ids is not b.own_plugin_install_ids
        a.active_downloads["m"] = object()
        a.own_plugin_install_ids.add("req-1")
        assert b.active_downloads == {}
        assert b.own_plugin_install_ids == set()


class TestDispatchTable:
    def test_every_type_the_chain_handled_resolves(self):
        expected = {EventType[name] for name in DISPATCHED_TYPES}
        assert set(websocket._DISPATCH) == expected

    def test_steer_is_not_in_the_table(self):
        # The loop's pre-pass owns STEER_TASK: it answers, enqueues
        # onto the live task, or rewrites the message to START_TASK
        # before the table is consulted.
        assert EventType.STEER_TASK not in websocket._DISPATCH

    def test_membership_pairs_share_one_handler(self):
        d = websocket._DISPATCH
        assert d[EventType.TOOL_APPROVAL_GRANTED] is \
            d[EventType.TOOL_APPROVAL_REJECTED]
        assert d[EventType.PROVIDER_VALIDATE_REQUEST] is \
            d[EventType.SETTINGS_PROVIDER_VALIDATE_REQUEST]
        assert d[EventType.PROJECT_CONFIG_GET_REQUEST] is \
            d[EventType.PROJECT_CONFIG_UPDATE_REQUEST]

    def test_handlers_are_coroutine_functions_128_distinct(self):
        handlers = list(websocket._DISPATCH.values())
        assert all(inspect.iscoroutinefunction(h) for h in handlers)
        # 123 at the Wave 5 split; +1 for the calendar layer's
        # CALENDAR_LIST_REQUEST handler (14 Nov 2026); +2 for the
        # Task Ledger read model's query + list handlers
        # (master build S3, 14 Nov 2026); +1 for the memory
        # backend picker's status handler (master build S9,
        # 14 Nov 2026); +1 for the machine capability report
        # handler (master build S10, 14 Nov 2026).
        assert len(set(handlers)) == 128


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


def _msg(event_type, req_id="req-1", payload=None):
    return Event(type=event_type, request_id=req_id,
                 payload=payload or {})


class TestDispositions:
    async def test_start_task_chains_a_launch_instead_of_awaiting(
            self, monkeypatch):
        # The launch disposition (RUX-B-07): the handler chains
        # start_task_message onto the connection's launch tail and
        # returns; it does not await the launch itself.
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def fake_start(conn_, msg_, req_id_, previous_start_):
            calls.append((conn_, msg_, req_id_, previous_start_))
            started.set()
            await release.wait()

        monkeypatch.setattr(websocket, "start_task_message", fake_start)
        conn = ws_connection.WsConnection(
            _FakeSocket(), object(), object())
        msg = _msg(EventType.START_TASK)
        await websocket._DISPATCH[EventType.START_TASK](conn, msg)
        # Handler returned while the launch is still pending.
        assert conn.task_start_tail is not None
        assert not conn.task_start_tail.done()
        await asyncio.wait_for(started.wait(), 1)
        assert calls == [(conn, msg, "req-1", None)]
        release.set()
        await conn.task_start_tail

    async def test_version_check_spawns_onto_the_background_registry(
            self, monkeypatch):
        # The spawn disposition (RUX-B-08): the answer is built
        # off the receive loop, registered for disconnect cleanup.
        release = asyncio.Event()
        ran = asyncio.Event()

        async def fake_result(conn_, req_id_):
            ran.set()
            await release.wait()

        monkeypatch.setattr(
            websocket, "send_version_check_result", fake_result)
        conn = ws_connection.WsConnection(
            _FakeSocket(), object(), object())
        await websocket._DISPATCH[EventType.VERSION_CHECK_REQUEST](
            conn, _msg(EventType.VERSION_CHECK_REQUEST))
        assert len(conn.background_tasks) == 1
        task = next(iter(conn.background_tasks))
        assert not task.done()
        release.set()
        await task
        assert ran.is_set()

    async def test_ping_answers_inline(self):
        ws = _FakeSocket()
        conn = ws_connection.WsConnection(ws, object(), object())
        await websocket._DISPATCH[EventType.PING](
            conn, _msg(EventType.PING, req_id="ping-9"))
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == EventType.PONG.value
        assert ws.sent[0]["request_id"] == "ping-9"

    async def test_cancel_with_no_active_task_answers_error(self):
        # The handler reads the connection's task state; with no
        # launch pending and no runner it answers honestly instead
        # of cancelling anything (N-44's other half).
        ws = _FakeSocket()
        conn = ws_connection.WsConnection(ws, object(), object())
        await websocket._DISPATCH[EventType.CANCEL_TASK](
            conn, _msg(EventType.CANCEL_TASK))
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == EventType.ERROR.value
        assert ws.sent[0]["payload"]["error"] == \
            "No active task to cancel"
