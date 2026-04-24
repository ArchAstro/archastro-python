"""
Phoenix Channel — manages a single topic subscription, push/reply, and events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .socket import Socket

logger = logging.getLogger("archastro.phx_channel")

DEFAULT_TIMEOUT_S = 10


class Channel:
    """
    Represents a single Phoenix Channel subscription on a topic.

    Created via ``socket.channel("topic", params)``.
    Call ``await channel.join()`` to subscribe.
    """

    def __init__(self, socket: Socket, topic: str, params: dict[str, Any]):
        self._socket = socket
        self._topic = topic
        self._params = params

        self._state: str = "closed"  # closed | joining | joined | leaving | errored
        self._join_ref: str | None = None
        self._join_push_ref: str | None = None

        self._event_handlers: dict[str, list[Callable[..., Any]]] = {}
        self._pending_replies: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._push_buffer: list[tuple[str, Any, asyncio.Future[dict[str, Any]]]] = []
        # Inbound pushes that arrived before any handler was registered.
        # Server scenarios can `autoPush` in response to a join frame, and
        # that push can land on the asyncio queue before the caller has
        # had a chance to register a handler post-join. We replay these
        # to the first matching handler registered within the window.
        # Bounded so a forgotten handler doesn't leak unbounded memory.
        self._pending_pushes: dict[str, list[Any]] = {}
        self._pending_pushes_cap: int = 32

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_joined(self) -> bool:
        return self._state == "joined"

    # ─── Join / Leave ─────────────────────────────────────────

    async def join(
        self,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """
        Join the channel. Returns the join response payload.

        ``payload`` overrides the params passed to ``socket.channel(topic, params)``
        when provided — generated SDK channel classes pass the join payload
        directly here, while hand-written callers typically rely on the params
        captured at channel creation time.

        Raises ``TimeoutError`` if the server doesn't reply within ``timeout``.
        Raises ``ChannelError`` if the server rejects the join.
        Raises ``ChannelError`` if the channel is already joined — callers
        should ``leave()`` before re-joining. Silently returning an empty
        response would let a double-call (e.g. two ``LiveDocChannel.join_*``
        invocations reusing the same underlying channel) masquerade as a
        successful join while dropping the real server response.
        """
        if self._state == "joined":
            raise ChannelError(f"Channel {self._topic} is already joined; call leave() first")

        self._state = "joining"
        ref = self._socket._make_ref()
        self._join_ref = ref
        self._join_push_ref = ref

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending_replies[ref] = future

        params = self._params if payload is None else payload
        await self._socket._send(ref, ref, self._topic, "phx_join", params)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending_replies.pop(ref, None)
            self._state = "errored"
            raise TimeoutError(f"Join timed out for {self._topic}") from None

        status = result.get("status")
        if status == "ok":
            self._state = "joined"
            logger.info("Joined %s", self._topic)
            # Flush buffered pushes
            await self._flush_push_buffer()
            return result.get("response", {})
        else:
            self._state = "errored"
            raise ChannelError(f"Join rejected for {self._topic}: {result.get('response', {})}")

    async def leave(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        """Leave the channel."""
        if self._state == "closed":
            return

        self._state = "leaving"
        ref = self._socket._make_ref()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending_replies[ref] = future

        await self._socket._send(self._join_ref, ref, self._topic, "phx_leave", {})

        try:
            await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            pass  # Leave is best-effort
        finally:
            self._state = "closed"
            self._join_ref = None
            self._socket._remove_channel(self._topic)
            logger.info("Left %s", self._topic)

    async def _rejoin(self) -> None:
        """Rejoin after reconnection."""
        if self._state in ("closed", "leaving"):
            return
        self._state = "closed"
        self._join_ref = None
        try:
            await self.join()
        except Exception as exc:
            logger.warning("Rejoin failed for %s: %s", self._topic, exc)
            self._state = "errored"

    # ─── Push / Reply ─────────────────────────────────────────

    async def push(
        self, event: str, payload: Any = None, timeout: float = DEFAULT_TIMEOUT_S
    ) -> dict[str, Any]:
        """
        Push an event to the channel and wait for a reply.

        Returns the reply ``{"status": ..., "response": ...}``.
        Raises ``TimeoutError`` if no reply within ``timeout``.
        """
        if payload is None:
            payload = {}

        if self._state != "joined":
            # Buffer the push for when we rejoin
            future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
            self._push_buffer.append((event, payload, future))
            return await asyncio.wait_for(future, timeout=timeout)

        return await self._do_push(event, payload, timeout)

    async def _do_push(self, event: str, payload: Any, timeout: float) -> dict[str, Any]:
        ref = self._socket._make_ref()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending_replies[ref] = future

        await self._socket._send(self._join_ref, ref, self._topic, event, payload)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending_replies.pop(ref, None)
            raise TimeoutError(f"Push '{event}' timed out on {self._topic}") from None

        return result

    async def _flush_push_buffer(self) -> None:
        buffer = self._push_buffer[:]
        self._push_buffer.clear()
        for event, payload, future in buffer:
            try:
                result = await self._do_push(event, payload, DEFAULT_TIMEOUT_S)
                if not future.done():
                    future.set_result(result)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

    # ─── Event handlers ──────────────────────────────────────

    def on(self, event: str, callback: Callable[..., Any]) -> Callable[[], None]:
        """
        Register a callback for a channel event.

        Returns an unsubscribe function. If pushes for this event already
        arrived before any handler was registered (e.g. server-side
        autoPush fired on join), they're replayed to this callback in
        arrival order so the caller can't miss them due to scheduling.
        """
        handlers = self._event_handlers.setdefault(event, [])
        handlers.append(callback)

        pending = self._pending_pushes.pop(event, None)
        if pending:
            for payload in pending:
                try:
                    callback(payload)
                except Exception:
                    logger.exception("Error in handler for %s:%s (replayed)", self._topic, event)

        def unsubscribe() -> None:
            handlers.remove(callback)

        return unsubscribe

    # ─── Internal message dispatch ────────────────────────────

    def _on_message(
        self,
        join_ref: str | None,
        ref: str | None,
        event: str,
        payload: Any,
    ) -> None:
        # Ignore messages from stale join
        if join_ref is not None and join_ref != self._join_ref:
            return

        if event == "phx_reply":
            self._handle_reply(ref, payload)
        elif event == "phx_close":
            self._handle_close()
        elif event == "phx_error":
            self._handle_error(payload)
        else:
            # User event — dispatch to handlers, or buffer if none yet.
            handlers = self._event_handlers.get(event)
            if handlers:
                for handler in handlers:
                    try:
                        handler(payload)
                    except Exception:
                        logger.exception("Error in handler for %s:%s", self._topic, event)
            else:
                buf = self._pending_pushes.setdefault(event, [])
                if len(buf) < self._pending_pushes_cap:
                    buf.append(payload)
                else:
                    # Drop the oldest to bound memory for orphan events.
                    buf.pop(0)
                    buf.append(payload)

    def _handle_reply(self, ref: str | None, payload: Any) -> None:
        if ref and ref in self._pending_replies:
            future = self._pending_replies.pop(ref)
            if not future.done():
                future.set_result(payload)

    def _handle_close(self) -> None:
        logger.info("Channel closed: %s", self._topic)
        self._state = "closed"
        self._trigger_event("phx_close", {})

    def _handle_error(self, payload: Any) -> None:
        logger.warning("Channel error: %s — %s", self._topic, payload)
        self._state = "errored"
        self._trigger_event("phx_error", payload)

    def _trigger_event(self, event: str, payload: Any) -> None:
        handlers = self._event_handlers.get(event, [])
        for handler in handlers:
            try:
                handler(payload)
            except Exception:
                logger.exception("Error in handler for %s:%s", self._topic, event)


class ChannelError(Exception):
    """Raised when a channel operation fails (e.g., join rejected)."""
