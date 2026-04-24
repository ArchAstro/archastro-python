"""
Smoke tests that drive the channel-harness service from the Python side.

Uses raw ``archastro.phx_channel.Socket`` + ``HarnessServiceClient`` — no generated
channel class involved. If these pass, the service's wire contract and the
Python runtime's reply/push handling are both sound, which is the prerequisite
for the emitted per-channel tests (added in a separate file by the generator).

The fixture spec is ``channel-harness-spec.json`` — the same one the TS
tests use, so any asymmetry between languages shows up as a failure here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from archastro.phx_channel import HarnessServiceClient, Socket
from archastro.phx_channel.channel import ChannelError


@pytest.fixture
async def client(harness_service):
    c = HarnessServiceClient(
        ws_url=harness_service["wsUrl"],
        control_url=harness_service["controlUrl"],
    )
    await c.reset()
    try:
        yield c
    finally:
        await c.close()


async def _join(socket: Socket, topic: str, payload: dict[str, Any]) -> dict[str, Any]:
    channel = socket.channel(topic)
    return await channel.join(payload)


async def test_service_synthesizes_a_contract_valid_join_reply(client):
    socket = await client.open_socket()
    resp = await _join(socket, "doc:doc_42", {"userId": "user_1"})

    assert set(resp.keys()) == {"document", "collaborators"}
    document = resp["document"]
    assert isinstance(document["id"], str)
    assert isinstance(document["content"], str)
    assert isinstance(document["version"], int)
    assert isinstance(resp["collaborators"], list)


async def test_register_scenario_reply_error_surfaces_as_channel_error(client):
    await client.register_scenario(
        {
            "topic": "doc:doc_42",
            "onJoin": [
                {"type": "replyError", "payload": {"reason": "locked"}},
            ],
        }
    )
    socket = await client.open_socket()
    with pytest.raises(ChannelError, match="locked"):
        await _join(socket, "doc:doc_42", {"userId": "user_1"})


async def test_observations_capture_inbound_message_params(client):
    await client.register_scenario(
        {
            "topic": "doc:doc_42",
            "onJoin": [{"type": "autoReply"}],
            "onMessage": {"edit": [{"type": "autoReply"}]},
        }
    )
    socket = await client.open_socket()
    channel = socket.channel("doc:doc_42")
    await channel.join({"userId": "user_1"})

    reply = await channel.push("edit", {"position": 4, "text": "yo"})
    assert reply["status"] == "ok"

    observed = await client.observations("doc:doc_42", "edit")
    assert len(observed) == 1
    assert observed[0]["params"] == {"position": 4, "text": "yo"}


async def test_autopush_reaches_the_python_handler(client):
    await client.register_scenario(
        {
            "topic": "doc:doc_42",
            "onJoin": [
                {"type": "autoReply"},
                {"type": "autoPush", "event": "user_joined"},
            ],
        }
    )
    socket = await client.open_socket()
    channel = socket.channel("doc:doc_42")
    received: list[Any] = []

    future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    def handler(payload):
        received.append(payload)
        if not future.done():
            future.set_result(payload)

    channel.on("user_joined", handler)
    await channel.join({"userId": "user_1"})

    payload = await asyncio.wait_for(future, timeout=1.0)
    assert set(payload.keys()) == {"id", "name"}
    assert isinstance(payload["id"], str)
    assert isinstance(payload["name"], str)
    assert received == [payload]


async def test_schema_invalid_push_params_reply_with_error_envelope(client):
    await client.register_scenario(
        {
            "topic": "doc:doc_42",
            "onJoin": [{"type": "autoReply"}],
        }
    )
    socket = await client.open_socket()
    channel = socket.channel("doc:doc_42")
    await channel.join({"userId": "user_1"})

    reply = await channel.push("edit", {})
    assert reply["status"] == "error"
    assert reply["response"]["reason"] == "invalid_params"


async def test_reset_clears_scenarios_and_observations(client):
    await client.register_scenario(
        {
            "topic": "doc:doc_42",
            "onJoin": [{"type": "autoReply"}],
        }
    )
    socket = await client.open_socket()
    await _join(socket, "doc:doc_42", {"userId": "user_1"})
    assert len(await client.observations()) > 0

    await client.reset()
    assert await client.observations() == []

    # After reset the topic has no scenario — default path synthesizes a reply.
    socket2 = await client.open_socket()
    resp = await _join(socket2, "doc:doc_42", {"userId": "user_1"})
    assert "document" in resp
