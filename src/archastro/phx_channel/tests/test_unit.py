"""
Unit tests for the Phoenix Channel client — no server required.
Tests protocol logic, state machine, message formatting, ref tracking, etc.
"""

import asyncio

import pytest

from archastro.phx_channel.channel import Channel, ChannelError
from archastro.phx_channel.socket import Socket


class MockSocket:
    """Minimal mock socket that records sent messages."""

    def __init__(self):
        self.sent: list[tuple] = []
        self._ref = 0
        self._channels: dict = {}

    def _make_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    async def _send(self, join_ref, ref, topic, event, payload):
        self.sent.append((join_ref, ref, topic, event, payload))

    def _remove_channel(self, topic):
        self._channels.pop(topic, None)


async def _joined_channel():
    """Helper: create a channel and join it."""
    socket = MockSocket()
    ch = Channel(socket, "test:topic", {})
    join_task = asyncio.create_task(ch.join(timeout=1))
    await asyncio.sleep(0.01)
    join_ref, ref, _, _, _ = socket.sent[0]
    ch._on_message(join_ref, ref, "phx_reply", {"status": "ok", "response": {}})
    await join_task
    socket.sent.clear()
    return socket, ch


# ─── Socket unit tests ──────────────────────────────────────────


def test_socket_starts_disconnected():
    s = Socket("ws://localhost:4000/socket/websocket")
    assert not s.is_connected


def test_socket_generates_incrementing_refs():
    s = Socket("ws://localhost:4000/socket/websocket")
    assert s._make_ref() == "1"
    assert s._make_ref() == "2"
    assert s._make_ref() == "3"


def test_socket_creates_channels_by_topic():
    s = Socket("ws://localhost:4000/socket/websocket")
    ch1 = s.channel("room:lobby")
    ch2 = s.channel("room:lobby")
    ch3 = s.channel("room:other")
    assert ch1 is ch2
    assert ch1 is not ch3


def test_socket_removes_channels():
    s = Socket("ws://localhost:4000/socket/websocket")
    ch = s.channel("room:lobby")
    s._remove_channel("room:lobby")
    ch2 = s.channel("room:lobby")
    assert ch2 is not ch


def test_socket_stores_config():
    s = Socket(
        "ws://localhost:4000/socket/websocket",
        heartbeat_interval=5,
        timeout=2,
        auto_reconnect=False,
        params={"token": "abc"},
    )
    # Verify params are stored (used in URL construction)
    assert s._params == {"token": "abc"}


def test_socket_on_callbacks_registrable():
    s = Socket("ws://localhost:4000/socket/websocket")
    called = []
    s.on_open(lambda: called.append("open"))
    s.on_close(lambda code, reason: called.append(("close", code, reason)))
    s.on_error(lambda exc: called.append(("error", exc)))
    # Just verify registration doesn't crash — actual invocation tested in integration


# ─── Channel state machine ──────────────────────────────────────


def test_initial_state():
    ch = Channel(MockSocket(), "test:topic", {})
    assert ch.state == "closed"
    assert not ch.is_joined


async def test_join_sends_phx_join():
    socket = MockSocket()
    ch = Channel(socket, "test:topic", {"key": "val"})
    join_task = asyncio.create_task(ch.join(timeout=1))
    await asyncio.sleep(0.01)

    assert len(socket.sent) == 1
    join_ref, ref, topic, event, payload = socket.sent[0]
    assert topic == "test:topic"
    assert event == "phx_join"
    assert payload == {"key": "val"}
    assert join_ref == ref

    ch._on_message(join_ref, ref, "phx_reply", {"status": "ok", "response": {"welcome": True}})
    response = await join_task
    assert response == {"welcome": True}
    assert ch.state == "joined"
    assert ch.is_joined


async def test_join_already_joined_raises():
    # Previously returned {} — but silent success dropped the real join
    # response, so a double-call (e.g. two generated-SDK `join_*` invocations
    # reusing the cached channel) masked a bug. Now the second call raises
    # so callers explicitly `leave()` before re-joining.
    _, ch = await _joined_channel()
    with pytest.raises(ChannelError, match="already joined"):
        await ch.join(timeout=1)


async def test_join_rejected():
    socket = MockSocket()
    ch = Channel(socket, "test:topic", {})
    join_task = asyncio.create_task(ch.join(timeout=1))
    await asyncio.sleep(0.01)

    join_ref, ref, _, _, _ = socket.sent[0]
    ch._on_message(
        join_ref, ref, "phx_reply", {"status": "error", "response": {"reason": "unauthorized"}}
    )

    with pytest.raises(ChannelError, match="unauthorized"):
        await join_task
    assert ch.state == "errored"


async def test_join_timeout():
    socket = MockSocket()
    ch = Channel(socket, "test:topic", {})
    with pytest.raises(TimeoutError, match="timed out"):
        await ch.join(timeout=0.05)


async def test_phx_close_transitions_to_closed():
    _, ch = await _joined_channel()
    ch._on_message(None, None, "phx_close", {})
    assert ch.state == "closed"


async def test_phx_error_transitions_to_errored():
    _, ch = await _joined_channel()
    ch._on_message(None, None, "phx_error", {"reason": "crash"})
    assert ch.state == "errored"


# ─── Push / Reply ────────────────────────────────────────────────


async def test_push_sends_correct_format():
    socket, ch = await _joined_channel()
    push_task = asyncio.create_task(ch.push("my_event", {"data": 123}, timeout=1))
    await asyncio.sleep(0.01)

    assert len(socket.sent) == 1
    join_ref, ref, topic, event, payload = socket.sent[0]
    assert topic == "test:topic"
    assert event == "my_event"
    assert payload == {"data": 123}

    ch._on_message(join_ref, ref, "phx_reply", {"status": "ok", "response": {"id": "abc"}})
    result = await push_task
    assert result == {"status": "ok", "response": {"id": "abc"}}


async def test_push_timeout():
    _, ch = await _joined_channel()
    with pytest.raises(TimeoutError, match="timed out"):
        await ch.push("slow", {}, timeout=0.05)


async def test_push_default_payload():
    socket, ch = await _joined_channel()
    push_task = asyncio.create_task(ch.push("evt", timeout=1))
    await asyncio.sleep(0.01)

    _, _, _, _, payload = socket.sent[0]
    assert payload == {}

    join_ref, ref, _, _, _ = socket.sent[0]
    ch._on_message(join_ref, ref, "phx_reply", {"status": "ok", "response": {}})
    await push_task


async def test_multiple_concurrent_pushes():
    socket, ch = await _joined_channel()

    p1 = asyncio.create_task(ch.push("evt_a", {"n": 1}, timeout=1))
    p2 = asyncio.create_task(ch.push("evt_b", {"n": 2}, timeout=1))
    await asyncio.sleep(0.01)

    assert len(socket.sent) == 2
    jr1, ref1, _, _, _ = socket.sent[0]
    jr2, ref2, _, _, _ = socket.sent[1]

    # Reply to second first
    ch._on_message(jr2, ref2, "phx_reply", {"status": "ok", "response": {"from": "b"}})
    ch._on_message(jr1, ref1, "phx_reply", {"status": "ok", "response": {"from": "a"}})

    r1 = await p1
    r2 = await p2
    assert r1["response"]["from"] == "a"
    assert r2["response"]["from"] == "b"


async def test_each_push_gets_unique_ref():
    socket, ch = await _joined_channel()
    for _ in range(3):
        asyncio.create_task(ch.push("evt", {}, timeout=1))
    await asyncio.sleep(0.01)

    refs = [m[1] for m in socket.sent]
    assert len(set(refs)) == 3


# ─── Event handlers ──────────────────────────────────────────────


async def test_event_dispatch():
    _, ch = await _joined_channel()
    received = []
    ch.on("my_event", lambda p: received.append(p))
    ch._on_message(None, None, "my_event", {"data": "hello"})
    assert received == [{"data": "hello"}]


async def test_multiple_handlers():
    _, ch = await _joined_channel()
    a, b = [], []
    ch.on("evt", lambda p: a.append(p))
    ch.on("evt", lambda p: b.append(p))
    ch._on_message(None, None, "evt", {"n": 1})
    assert len(a) == 1
    assert len(b) == 1


async def test_unsubscribe():
    _, ch = await _joined_channel()
    a, b = [], []
    unsub = ch.on("evt", lambda p: a.append(p))
    ch.on("evt", lambda p: b.append(p))
    unsub()
    ch._on_message(None, None, "evt", {"n": 1})
    assert len(a) == 0
    assert len(b) == 1


async def test_stale_join_ref_ignored():
    _, ch = await _joined_channel()
    received = []
    ch.on("evt", lambda p: received.append(p))
    ch._on_message("wrong_ref", None, "evt", {"data": "stale"})
    assert len(received) == 0


async def test_null_join_ref_accepted():
    _, ch = await _joined_channel()
    received = []
    ch.on("evt", lambda p: received.append(p))
    ch._on_message(None, None, "evt", {"data": "broadcast"})
    assert received == [{"data": "broadcast"}]


async def test_pushes_before_handler_are_replayed_on_registration():
    """
    Server `autoPush` can fire in response to a join frame and land on the
    asyncio queue before the caller has registered a handler. Verify the
    Channel buffers those pushes and replays them when on() is called.
    """
    _, ch = await _joined_channel()
    # Two pushes arrive with no handlers yet — both should be buffered.
    ch._on_message(None, None, "new_entry", {"id": "a"})
    ch._on_message(None, None, "new_entry", {"id": "b"})
    received: list[dict] = []
    ch.on("new_entry", lambda p: received.append(p))
    assert received == [{"id": "a"}, {"id": "b"}]
    # Subsequent pushes go straight through.
    ch._on_message(None, None, "new_entry", {"id": "c"})
    assert received == [{"id": "a"}, {"id": "b"}, {"id": "c"}]


async def test_pending_push_buffer_is_bounded():
    _, ch = await _joined_channel()
    ch._pending_pushes_cap = 3
    for i in range(5):
        ch._on_message(None, None, "evt", {"i": i})
    received: list[dict] = []
    ch.on("evt", lambda p: received.append(p))
    # Oldest dropped once cap hit; newest kept in order.
    assert received == [{"i": 2}, {"i": 3}, {"i": 4}]


async def test_handler_error_does_not_crash_dispatch():
    _, ch = await _joined_channel()
    received = []
    ch.on("evt", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    ch.on("evt", lambda p: received.append(p))
    # Should not raise
    ch._on_message(None, None, "evt", {"data": "test"})
    assert received == [{"data": "test"}]


async def test_phx_close_fires_handlers():
    _, ch = await _joined_channel()
    events = []
    ch.on("phx_close", lambda p: events.append(p))
    ch._on_message(None, None, "phx_close", {})
    assert len(events) == 1


async def test_phx_error_fires_handlers():
    _, ch = await _joined_channel()
    events = []
    ch.on("phx_error", lambda p: events.append(p))
    ch._on_message(None, None, "phx_error", {"reason": "crash"})
    assert events == [{"reason": "crash"}]


# ─── Push buffering ──────────────────────────────────────────────


async def test_buffers_pushes_before_join():
    socket = MockSocket()
    ch = Channel(socket, "test:topic", {})

    push_task = asyncio.create_task(ch.push("buffered", {"n": 1}, timeout=5))
    await asyncio.sleep(0.05)

    # Reply to buffered push shortly after join succeeds
    async def reply_to_buffered():
        # Wait for the buffered push to actually be sent
        for _ in range(50):
            push_msgs = [m for m in socket.sent if m[3] == "buffered"]
            if push_msgs:
                msg = push_msgs[0]
                ch._on_message(
                    msg[0], msg[1], "phx_reply", {"status": "ok", "response": {"buffered": True}}
                )
                return
            await asyncio.sleep(0.05)

    join_task = asyncio.create_task(ch.join(timeout=5))
    reply_task = asyncio.create_task(reply_to_buffered())
    await asyncio.sleep(0.05)

    # Complete the join
    join_ref, ref, _, _, _ = socket.sent[0]
    ch._on_message(join_ref, ref, "phx_reply", {"status": "ok", "response": {}})
    await join_task

    # Wait for both the reply and the push to resolve
    await reply_task
    result = await push_task
    assert result == {"status": "ok", "response": {"buffered": True}}


# ─── Leave ───────────────────────────────────────────────────────


async def test_leave():
    socket, ch = await _joined_channel()
    leave_task = asyncio.create_task(ch.leave(timeout=1))
    await asyncio.sleep(0.01)

    assert len(socket.sent) == 1
    _, _, _, event, _ = socket.sent[0]
    assert event == "phx_leave"

    join_ref, ref, _, _, _ = socket.sent[0]
    ch._on_message(join_ref, ref, "phx_reply", {"status": "ok", "response": {}})
    await leave_task
    assert ch.state == "closed"


async def test_leave_on_closed_is_noop():
    socket = MockSocket()
    ch = Channel(socket, "test:topic", {})
    await ch.leave(timeout=1)
    assert len(socket.sent) == 0


async def test_leave_timeout_graceful():
    _, ch = await _joined_channel()
    await ch.leave(timeout=0.05)
    assert ch.state == "closed"


# ─── Rejoin ──────────────────────────────────────────────────────


async def test_rejoin_resets_and_joins():
    socket, ch = await _joined_channel()
    assert ch.is_joined

    rejoin_task = asyncio.create_task(ch._rejoin())
    await asyncio.sleep(0.01)

    join_msg = [m for m in socket.sent if m[3] == "phx_join"]
    assert len(join_msg) == 1

    ch._on_message(
        join_msg[0][0],
        join_msg[0][1],
        "phx_reply",
        {"status": "ok", "response": {"rejoined": True}},
    )
    await rejoin_task
    assert ch.is_joined


async def test_rejoin_on_closed_is_noop():
    socket = MockSocket()
    ch = Channel(socket, "test:topic", {})
    await ch._rejoin()
    assert len(socket.sent) == 0


async def test_rejoin_sets_errored_on_failure():
    socket, ch = await _joined_channel()
    rejoin_task = asyncio.create_task(ch._rejoin())
    await asyncio.sleep(0.01)

    join_msg = [m for m in socket.sent if m[3] == "phx_join"]
    ch._on_message(
        join_msg[0][0],
        join_msg[0][1],
        "phx_reply",
        {"status": "error", "response": {"reason": "gone"}},
    )
    await rejoin_task
    assert ch.state == "errored"
