from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def load_example_module():
    module_path = Path(__file__).parents[2] / "examples" / "thread_chat_tui" / "main.py"
    spec = importlib.util.spec_from_file_location("thread_chat_tui_main", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_args_uses_platform_env_names(monkeypatch):
    module = load_example_module()

    monkeypatch.setenv("ARCHASTRO_PLATFORM_BASE_URL", "http://localhost:4005")
    monkeypatch.setenv("ARCHASTRO_PLATFORM_WS_URL", "ws://localhost:4005/socket/api/websocket")

    args = module.parse_args(["thr_123"])

    assert args.base_url == "http://localhost:4005"
    assert args.ws_url == "ws://localhost:4005/socket/api/websocket"


def test_parse_args_accepts_positional_thread_and_team_scope():
    module = load_example_module()

    args = module.parse_args(["thr_123", "--team", "team_123", "--limit", "10"])

    assert args.thread_id == "thr_123"
    assert args.team_id == "team_123"
    assert args.limit == 10


def test_message_from_payload_prefers_actor_name_and_content():
    module = load_example_module()

    message = module.message_from_payload(
        {
            "message": {
                "id": "msg_123",
                "actors": [{"name": "Ada Lovelace", "id": "user_123"}],
                "content": "hello from the websocket",
                "created_at": "2026-06-11T12:00:00Z",
            }
        }
    )

    assert message.id == "msg_123"
    assert message.author == "Ada Lovelace"
    assert message.content == "hello from the websocket"


def test_render_message_wraps_for_terminal_width():
    module = load_example_module()
    message = module.ChatMessage(
        id="msg_123",
        author="Ada",
        content="one two three four five six",
        created_at=None,
    )

    lines = module.render_message(message, width=18)

    assert lines == ["Ada: one two three", "  four five six"]


def test_draw_avoids_bottom_right_curses_cell():
    module = load_example_module()
    tui = module.ThreadChatTui(FakeSession(), thread_id="thr_123", team_id=None)
    tui._draft = "hello from the bottom row"

    screen = FakeCursesScreen(module.curses, height=8, width=32)

    tui._draw(screen)

    bottom_writes = [write for write in screen.writes if write[0] == 7]
    assert bottom_writes
    assert all(write[3] < 32 for write in bottom_writes)


@pytest.mark.asyncio
async def test_tui_sends_messages_through_session_boundary():
    module = load_example_module()
    session = FakeSession()
    tui = module.ThreadChatTui(session, thread_id="thr_123", team_id=None)

    await tui._post_message("hello through the session")

    assert session.sent == [
        {
            "content": "hello through the session",
            "idempotency_key": tui._messages[0].idempotency_key,
        }
    ]
    assert tui._messages[0].state == "sent"
    assert tui._status == "Message sent."


@pytest.mark.asyncio
async def test_chat_joins_user_thread_and_cleans_up(monkeypatch):
    module = load_example_module()

    socket = FakeSocket()
    channel = FakeChannel()
    clients = []
    joins = []

    class FakeAsyncPlatformClient:
        def __init__(self, *, api_key, access_token, base_url):
            self.api_key = api_key
            self.access_token = access_token
            self.base_url = base_url
            self.open_socket_calls = []
            self.closed = False
            clients.append(self)

        @classmethod
        def with_token(cls, api_key, access_token, *, base_url=None):
            return cls(api_key=api_key, access_token=access_token, base_url=base_url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.closed = True

        async def open_socket(self, *, url=None):
            self.open_socket_calls.append({"url": url})
            return socket

    class FakeApiChatChannel:
        @staticmethod
        async def join_user_thread(socket_arg, thread_id, *, include_metadata, limit):
            joins.append((socket_arg, thread_id, include_metadata, limit))
            return channel

    monkeypatch.setenv("ARCHASTRO_ACCESS_TOKEN", "sat_test")
    monkeypatch.setenv("ARCHASTRO_API_KEY", "pk_test")
    monkeypatch.setattr(module, "AsyncPlatformClient", FakeAsyncPlatformClient)
    monkeypatch.setattr(module, "ApiChatChannel", FakeApiChatChannel)
    monkeypatch.setattr(module, "ThreadChatTui", FakeTui)

    args = module.parse_args(
        [
            "thr_123",
            "--base-url",
            "http://localhost:4000",
            "--ws-url",
            "ws://localhost:4000/socket/api/websocket",
            "--limit",
            "5",
        ]
    )

    await module.chat(args)

    assert clients[0].api_key == "pk_test"
    assert clients[0].access_token == "sat_test"
    assert clients[0].base_url == "http://localhost:4000"
    assert clients[0].open_socket_calls == [{"url": "ws://localhost:4000/socket/api/websocket"}]
    assert clients[0].closed is True
    assert joins == [(socket, "thr_123", True, 5)]
    assert isinstance(FakeTui.instances[0].session, module.ThreadChatSession)
    assert channel.message_handler is not None
    assert channel.left is True


@pytest.mark.asyncio
async def test_chat_closes_client_when_leave_raises(monkeypatch):
    module = load_example_module()

    socket = FakeSocket()
    channel = FakeChannel(leave_error=RuntimeError("leave failed"))
    clients = []

    class FakeAsyncPlatformClient:
        @classmethod
        def with_token(cls, api_key, access_token, *, base_url=None):
            client = cls()
            clients.append(client)
            return client

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.closed = True

        async def open_socket(self, *, url=None):
            return socket

    class FakeApiChatChannel:
        @staticmethod
        async def join_user_thread(socket_arg, thread_id, *, include_metadata, limit):
            return channel

    monkeypatch.setenv("ARCHASTRO_ACCESS_TOKEN", "sat_test")
    monkeypatch.setenv("ARCHASTRO_API_KEY", "pk_test")
    monkeypatch.setattr(module, "AsyncPlatformClient", FakeAsyncPlatformClient)
    monkeypatch.setattr(module, "ApiChatChannel", FakeApiChatChannel)
    monkeypatch.setattr(module, "ThreadChatTui", FakeTui)

    args = module.parse_args(["thr_123"])

    with pytest.raises(RuntimeError, match="leave failed"):
        await module.chat(args)

    assert clients[0].closed is True


class FakeSocket:
    pass


class FakeChannel:
    def __init__(self, *, leave_error=None):
        self.leave_error = leave_error
        self.message_handler = None
        self.left = False

    def on_message_added(self, callback):
        self.message_handler = callback

    async def api_chat_list_messages(self, payload):
        return {"response": {"messages": []}}

    async def leave(self):
        self.left = True
        if self.leave_error:
            raise self.leave_error


class FakeSession:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, *, idempotency_key):
        self.sent.append({"content": content, "idempotency_key": idempotency_key})


class FakeTui:
    instances = []

    def __init__(self, session, *, thread_id, team_id):
        self.session = session
        self.thread_id = thread_id
        self.team_id = team_id
        self.history = None
        FakeTui.instances.append(self)

    def add_history(self, messages):
        self.history = messages

    def add_message_payload(self, payload):
        pass

    def set_status(self, status):
        pass

    async def run(self):
        pass


class FakeCursesScreen:
    def __init__(self, curses_module, *, height, width):
        self._curses = curses_module
        self._height = height
        self._width = width
        self.writes = []
        self.cursor = None
        self.refreshed = False

    def getmaxyx(self):
        return self._height, self._width

    def erase(self):
        pass

    def addnstr(self, y, x, text, n, *attrs):
        if y == self._height - 1 and x + n >= self._width:
            raise self._curses.error("addnwstr() returned ERR")
        self.writes.append((y, x, text, n, attrs))

    def move(self, y, x):
        self.cursor = (y, x)

    def refresh(self):
        self.refreshed = True
