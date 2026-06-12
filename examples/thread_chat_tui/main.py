# Copyright (c) 2026 ArchAstro Inc. All Rights Reserved.

from __future__ import annotations

import argparse
import asyncio
import curses
import os
import textwrap
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from archastro.platform import AsyncPlatformClient
from archastro.platform.channels.api_chat_channel import ApiChatChannel

DEFAULT_PLATFORM_BASE_URL = "https://platform.archastro.ai"


@dataclass(frozen=True)
class ChatMessage:
    id: str
    author: str
    content: str
    created_at: str | None = None
    idempotency_key: str | None = None
    state: str | None = None


class ThreadChatSession:
    def __init__(self, channel: ApiChatChannel):
        self._channel = channel

    def on_message_added(self, callback: Callable[[dict[str, object]], None]) -> Callable[[], None]:
        return self._channel.on_message_added(callback)

    async def load_history(self) -> list[ChatMessage]:
        reply = await self._channel.api_chat_list_messages({})
        return messages_from_reply(reply)

    async def send_message(self, content: str, *, idempotency_key: str) -> None:
        reply = await self._channel.api_chat_post_simple_message(
            {"content": content, "idempotency_key": idempotency_key}
        )
        if reply.get("status") == "ok":
            return
        raise RuntimeError(f"Send rejected: {reply.get('response', reply)}")

    async def close(self) -> None:
        await self._channel.leave()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chat in an existing ArchAstro thread over the websocket SDK."
    )
    parser.add_argument(
        "thread_id", metavar="THREAD_ID", help="Thread id to join, for example thr_..."
    )
    parser.add_argument(
        "--team",
        dest="team_id",
        help="Join the thread as a team-scoped chat channel instead of a user-scoped channel.",
    )
    parser.add_argument(
        "--base-url",
        default=_env("ARCHASTRO_PLATFORM_BASE_URL", "ARCHASTRO_BASE_URL")
        or DEFAULT_PLATFORM_BASE_URL,
        help=(
            "Platform base URL. Defaults to ARCHASTRO_PLATFORM_BASE_URL, "
            "ARCHASTRO_BASE_URL, or production."
        ),
    )
    parser.add_argument(
        "--ws-url",
        default=_env("ARCHASTRO_PLATFORM_WS_URL"),
        help=(
            "Optional websocket URL override. By default AsyncPlatformClient derives "
            "the /socket/api/websocket URL from --base-url."
        ),
    )
    parser.add_argument(
        "--api-key",
        default=_env("ARCHASTRO_API_KEY"),
        help="Publishable app API key. Defaults to ARCHASTRO_API_KEY.",
    )
    parser.add_argument(
        "--access-token",
        default=_env("ARCHASTRO_ACCESS_TOKEN"),
        help="User access token. Defaults to ARCHASTRO_ACCESS_TOKEN.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Initial message limit requested when joining the channel.",
    )
    return parser.parse_args(argv)


def message_from_payload(payload: dict[str, object]) -> ChatMessage:
    raw_message = payload.get("message", payload)
    if not isinstance(raw_message, dict):
        raw_message = {}

    return ChatMessage(
        id=str(raw_message.get("id") or f"message:{uuid.uuid4()}"),
        author=author_for(raw_message),
        content=str(raw_message.get("content") or ""),
        created_at=_string_or_none(raw_message.get("created_at")),
        idempotency_key=_string_or_none(raw_message.get("idempotency_key")),
    )


def author_for(message: dict[str, object]) -> str:
    actors = message.get("actors")
    if isinstance(actors, list) and actors:
        actor = actors[0]
        if isinstance(actor, dict):
            for key in ("name", "alias", "id"):
                value = actor.get(key)
                if isinstance(value, str) and value:
                    return value

    user = message.get("user")
    if isinstance(user, dict):
        for key in ("name", "alias", "id"):
            value = user.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(user, str) and user:
        return user

    agent = message.get("agent")
    if isinstance(agent, str) and agent:
        return agent

    return "unknown"


def render_message(message: ChatMessage, width: int) -> list[str]:
    state = f" [{message.state}]" if message.state else ""
    content = f"{message.content}{state}".strip()
    prefix = f"{message.author}: "
    first_width = max(8, width - len(prefix))
    later_width = max(8, width - 2)
    wrapped = textwrap.wrap(content, width=first_width) or [""]

    lines = [f"{prefix}{wrapped[0]}"]
    for chunk in textwrap.wrap(" ".join(wrapped[1:]), width=later_width):
        lines.append(f"  {chunk}")
    return [line[:width] for line in lines]


def messages_from_reply(reply: dict[str, object]) -> list[ChatMessage]:
    payload = reply.get("response", reply)
    messages = _messages_list(payload)
    return [message_from_payload({"message": message}) for message in messages]


def _messages_list(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("messages", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _messages_list(value)
            if nested:
                return nested
    return []


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class ThreadChatTui:
    def __init__(self, session: ThreadChatSession, *, thread_id: str, team_id: str | None):
        self._session = session
        self._thread_id = thread_id
        self._team_id = team_id
        self._messages: list[ChatMessage] = []
        self._seen_message_ids: set[str] = set()
        self._draft = ""
        self._status = "Connected. Enter sends, Ctrl-D or Ctrl-C exits."
        self._running = True
        self._dirty = True
        self._send_tasks: set[asyncio.Task[None]] = set()

    def add_history(self, messages: list[ChatMessage]) -> None:
        for message in messages:
            self._append_or_replace(message)
        self._dirty = True

    def add_message_payload(self, payload: dict[str, object]) -> None:
        self._append_or_replace(message_from_payload(payload))
        self._dirty = True

    def set_status(self, status: str) -> None:
        self._status = status
        self._dirty = True

    async def run(self) -> None:
        screen = None

        try:
            screen = curses.initscr()
            curses.noecho()
            curses.cbreak()
            with suppress(curses.error):
                curses.curs_set(1)
            screen.keypad(True)
            screen.nodelay(True)

            while self._running:
                self._read_keys(screen)
                if self._dirty:
                    self._draw(screen)
                await asyncio.sleep(0.03)
        finally:
            for task in self._send_tasks:
                task.cancel()
            if self._send_tasks:
                await asyncio.gather(*self._send_tasks, return_exceptions=True)
            if screen is not None:
                with suppress(curses.error):
                    screen.nodelay(False)
                with suppress(curses.error):
                    screen.keypad(False)
            with suppress(curses.error):
                curses.nocbreak()
            with suppress(curses.error):
                curses.echo()
            with suppress(curses.error):
                curses.endwin()

    def _read_keys(self, screen: Any) -> None:
        while True:
            try:
                key = screen.get_wch()
            except curses.error:
                return

            if key in ("\x03", "\x04"):
                self._running = False
                self._dirty = True
                return
            if key in ("\n", "\r"):
                self._submit_draft()
                continue
            if key in ("\b", "\x7f") or key == curses.KEY_BACKSPACE:
                self._draft = self._draft[:-1]
                self._dirty = True
                continue
            if key == curses.KEY_RESIZE:
                self._dirty = True
                continue
            if isinstance(key, str) and key.isprintable():
                self._draft += key
                self._dirty = True

    def _submit_draft(self) -> None:
        content = self._draft.strip()
        self._draft = ""
        self._dirty = True
        if not content:
            return
        task = asyncio.create_task(self._post_message(content))
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    async def _post_message(self, content: str) -> None:
        idempotency_key = str(uuid.uuid4())
        local = ChatMessage(
            id=f"local:{idempotency_key}",
            author="you",
            content=content,
            idempotency_key=idempotency_key,
            state="sending",
        )
        self._messages.append(local)
        self._dirty = True

        try:
            await self._session.send_message(content, idempotency_key=idempotency_key)
        except Exception as exc:
            self._replace_pending(idempotency_key, replace(local, state="failed"))
            self._status = f"Send failed: {exc}"
            self._dirty = True
            return

        self._replace_pending(idempotency_key, replace(local, state="sent"))
        self._status = "Message sent."
        self._dirty = True

    def _append_or_replace(self, message: ChatMessage) -> None:
        if message.idempotency_key and self._replace_pending(message.idempotency_key, message):
            self._seen_message_ids.add(message.id)
            return
        if message.id in self._seen_message_ids:
            return
        self._messages.append(message)
        self._seen_message_ids.add(message.id)

    def _replace_pending(self, idempotency_key: str, replacement: ChatMessage) -> bool:
        for index, message in enumerate(self._messages):
            if message.idempotency_key == idempotency_key:
                self._messages[index] = replacement
                return True
        return False

    def _draw(self, screen: Any) -> None:
        height, width = screen.getmaxyx()
        width = max(20, width)
        screen.erase()

        if height < 4:
            screen.addnstr(0, 0, "Make the terminal taller to chat.".ljust(width), width)
            screen.refresh()
            self._dirty = False
            return

        scope = f"team {self._team_id}" if self._team_id else "user"
        header = f" ArchAstro chat | {scope} | {self._thread_id} "
        screen.addnstr(0, 0, header.ljust(width), width, curses.A_REVERSE)
        screen.addnstr(1, 0, self._status.ljust(width), width)

        body_top = 2
        body_bottom = max(body_top, height - 2)
        body_height = max(1, body_bottom - body_top)
        lines = self._render_transcript(width)
        visible_lines = lines[-body_height:]
        for offset, line in enumerate(visible_lines):
            screen.addnstr(body_top + offset, 0, line.ljust(width), width)

        prompt = f"> {self._draft}"
        prompt_width = max(1, width - 1)
        if len(prompt) > prompt_width:
            prompt = "> " + self._draft[-(prompt_width - 2) :]
        screen.addnstr(height - 1, 0, prompt.ljust(prompt_width), prompt_width, curses.A_BOLD)
        screen.move(height - 1, min(len(prompt), prompt_width - 1))
        screen.refresh()
        self._dirty = False

    def _render_transcript(self, width: int) -> list[str]:
        if not self._messages:
            return ["No messages loaded yet. Type a message and press Enter."]

        lines: list[str] = []
        for message in self._messages:
            lines.extend(render_message(message, width))
            lines.append("")
        return lines[:-1]


async def chat(args: argparse.Namespace) -> None:
    if not args.api_key:
        raise SystemExit("Set ARCHASTRO_API_KEY before running this example.")
    if not args.access_token:
        raise SystemExit("Set ARCHASTRO_ACCESS_TOKEN before running this example.")

    async with AsyncPlatformClient.with_token(
        args.api_key,
        args.access_token,
        base_url=args.base_url,
    ) as client:
        socket = await client.open_socket(url=args.ws_url)
        session: ThreadChatSession | None = None

        try:
            if args.team_id:
                channel = await ApiChatChannel.join_team_thread(
                    socket,
                    args.team_id,
                    args.thread_id,
                    include_metadata=True,
                    limit=args.limit,
                )
            else:
                channel = await ApiChatChannel.join_user_thread(
                    socket,
                    args.thread_id,
                    include_metadata=True,
                    limit=args.limit,
                )

            session = ThreadChatSession(channel)
            tui = ThreadChatTui(session, thread_id=args.thread_id, team_id=args.team_id)
            session.on_message_added(tui.add_message_payload)

            try:
                history = await session.load_history()
            except Exception as exc:
                history = []
                tui.set_status(f"Connected. History unavailable: {exc}")
            tui.add_history(history)

            await tui.run()
        finally:
            if session is not None:
                await session.close()


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def main() -> None:
    asyncio.run(chat(parse_args()))


if __name__ == "__main__":
    main()
