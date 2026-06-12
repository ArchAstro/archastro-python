#!/usr/bin/env python3
"""Smoke-test generated Python channel helpers against a live platform-rs instance."""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from typing import Any

try:
    from archastro.platform import AsyncPlatformClient
    from archastro.platform.channels import ApiChatChannel
except ImportError as exc:  # pragma: no cover - exercised only before regeneration.
    raise SystemExit(
        "This smoke script requires regenerated SDK files with AsyncPlatformClient. "
        "Run ./scripts/regenerate_sdk.sh with the updated sdk-generator first."
    ) from exc


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Join an authenticated chat channel through AsyncPlatformClient.open_socket()."
    )
    parser.add_argument(
        "--base-url",
        default=_env("ARCHASTRO_PLATFORM_BASE_URL", "http://127.0.0.1:4000"),
        help="HTTP base URL for platform-rs. Defaults to ARCHASTRO_PLATFORM_BASE_URL.",
    )
    parser.add_argument(
        "--ws-url",
        default=_env("ARCHASTRO_PLATFORM_WS_URL"),
        help="Optional explicit websocket URL. Defaults to base URL converted to /socket/api/websocket.",
    )
    parser.add_argument(
        "--api-key",
        default=_env("ARCHASTRO_API_KEY"),
        help="Publishable app key. Defaults to ARCHASTRO_API_KEY.",
    )
    parser.add_argument(
        "--access-token",
        default=_env("ARCHASTRO_ACCESS_TOKEN"),
        help="User access token. Defaults to ARCHASTRO_ACCESS_TOKEN.",
    )
    parser.add_argument(
        "--thread-id",
        default=_env("ARCHASTRO_THREAD_ID"),
        help="Join an existing user thread by ID instead of joining by key.",
    )
    parser.add_argument(
        "--key",
        default=_env("ARCHASTRO_THREAD_KEY", "python-sdk-channel-smoke"),
        help="Key for the default user keyed thread join.",
    )
    parser.add_argument(
        "--transient",
        action="store_true",
        help="Join a transient user thread by key instead of a persisted keyed thread.",
    )
    parser.add_argument(
        "--message",
        default=_env("ARCHASTRO_CHANNEL_SMOKE_MESSAGE", "python sdk channel smoke"),
        help="Message content to send after joining.",
    )
    parser.add_argument(
        "--skip-post",
        action="store_true",
        help="Only join and list messages; do not send a smoke message.",
    )
    return parser


def _assert_ok_reply(name: str, reply: dict[str, Any]) -> dict[str, Any]:
    status = reply.get("status")
    if status == "error":
        raise RuntimeError(f"{name} failed: {reply.get('response')!r}")
    if status != "ok":
        raise RuntimeError(f"{name} returned unexpected status: {status!r}")
    response = reply.get("response")
    if not isinstance(response, dict):
        raise RuntimeError(f"{name} returned malformed response: {response!r}")
    return response


def _message_ids(response: dict[str, Any]) -> set[str]:
    messages = response.get("messages")
    if not isinstance(messages, list):
        raise RuntimeError(f"list_messages returned malformed messages: {messages!r}")
    ids: set[str] = set()
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("id"), str):
            ids.add(message["id"])
    return ids


async def _join_chat_channel(
    socket: Any,
    *,
    thread_id: str | None,
    key: str,
    transient: bool,
) -> ApiChatChannel:
    if thread_id:
        return await ApiChatChannel.join_user_thread(
            socket,
            thread_id,
            include_metadata=True,
            limit=5,
        )
    if transient:
        return await ApiChatChannel.join_user_transient(
            socket,
            key,
            include_metadata=True,
            limit=5,
        )
    return await ApiChatChannel.join_user_keyed(socket, key, include_metadata=True, limit=5)


async def _run(args: argparse.Namespace) -> None:
    if not args.api_key:
        raise SystemExit("Missing --api-key or ARCHASTRO_API_KEY")
    if not args.access_token:
        raise SystemExit("Missing --access-token or ARCHASTRO_ACCESS_TOKEN")

    async with AsyncPlatformClient.with_token(
        args.api_key,
        args.access_token,
        base_url=args.base_url,
    ) as client:
        socket = await client.open_socket(url=args.ws_url, auto_reconnect=False)
        channel = await _join_chat_channel(
            socket,
            thread_id=args.thread_id,
            key=args.key,
            transient=args.transient,
        )
        try:
            print(f"joined chat channel; join_response_keys={sorted(channel.join_response.keys())}")

            listed = await channel.api_chat_list_messages({})
            listed_response = _assert_ok_reply("list_messages", listed)
            print(f"list_messages status=ok count={len(listed_response.get('messages', []))}")

            if not args.skip_post:
                posted = await channel.api_chat_post_simple_message(
                    {
                        "content": args.message,
                        "idempotency_key": f"python-sdk-smoke-{uuid.uuid4()}",
                    }
                )
                posted_response = _assert_ok_reply("post_simple_message", posted)
                message = posted_response.get("message") or {}
                message_id = message.get("id")
                if not isinstance(message_id, str):
                    raise RuntimeError(
                        f"post_simple_message returned malformed message: {message!r}"
                    )
                print(f"post_simple_message status=ok id={message_id}")

                readback = await channel.api_chat_list_messages({})
                readback_response = _assert_ok_reply("readback list_messages", readback)
                readback_ids = _message_ids(readback_response)
                if message_id not in readback_ids:
                    raise RuntimeError(
                        f"readback missing posted message {message_id}; saw {sorted(readback_ids)}"
                    )
                print(f"readback status=ok found={message_id}")
        finally:
            await channel.leave()


def main() -> None:
    args = _parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
