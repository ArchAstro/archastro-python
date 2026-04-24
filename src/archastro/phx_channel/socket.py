"""
Phoenix Socket — manages the WebSocket connection, heartbeat, and channels.

Wire protocol: JSON arrays [join_ref, ref, topic, event, payload]
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import websockets
from websockets.asyncio.client import ClientConnection

from .channel import Channel

logger = logging.getLogger("archastro.phx_channel")

# Default backoff schedule (milliseconds) matching the Phoenix JS client
DEFAULT_RECONNECT_BACKOFF_MS = [10, 50, 100, 150, 200, 250, 500, 1000, 2000]
DEFAULT_HEARTBEAT_INTERVAL_S = 30
DEFAULT_TIMEOUT_S = 10


class Socket:
    """
    Manages a WebSocket connection to a Phoenix server.

    Usage::

        socket = Socket("ws://localhost:4000/socket/websocket", params={"token": "..."})
        await socket.connect()

        channel = socket.channel("room:lobby", {"user_id": "123"})
        resp = await channel.join()

        await channel.push("new_msg", {"body": "hello"})
        channel.on("new_msg", lambda payload: print(payload))

        await socket.disconnect()
    """

    def __init__(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        timeout: float = DEFAULT_TIMEOUT_S,
        reconnect_backoff_ms: list[int] | None = None,
        auto_reconnect: bool = True,
    ):
        self._base_url = url
        self._params = params or {}
        self._heartbeat_interval = heartbeat_interval
        self._timeout = timeout
        self._backoff = reconnect_backoff_ms or DEFAULT_RECONNECT_BACKOFF_MS
        self._auto_reconnect = auto_reconnect

        self._ws: ClientConnection | None = None
        self._ref = 0
        self._pending_heartbeat_ref: str | None = None
        self._channels: dict[str, Channel] = {}
        self._connected = False
        self._closing = False

        self._heartbeat_task: asyncio.Task[None] | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._reconnect_attempt = 0

        self._on_open_callbacks: list[Callable[[], Any]] = []
        self._on_close_callbacks: list[Callable[[int, str], Any]] = []
        self._on_error_callbacks: list[Callable[[Exception], Any]] = []

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    def _make_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    def _build_url(self) -> str:
        parsed = urlparse(self._base_url)
        params = {**self._params, "vsn": "2.0.0"}
        # Merge any existing query params
        existing_qs = parsed.query
        if existing_qs:
            qs = existing_qs + "&" + urlencode(params)
        else:
            qs = urlencode(params)
        return urlunparse(parsed._replace(query=qs))

    # ─── Connection lifecycle ─────────────────────────────────

    async def connect(self) -> None:
        """Connect to the Phoenix server."""
        self._closing = False
        self._reconnect_attempt = 0
        await self._do_connect()

    async def _do_connect(self) -> None:
        url = self._build_url()
        try:
            self._ws = await websockets.connect(url)
            self._connected = True
            self._reconnect_attempt = 0
            logger.info("Connected to %s", self._base_url)

            for cb in self._on_open_callbacks:
                cb()

            # Start heartbeat and receive loops
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Rejoin any channels that were previously joined
            for channel in self._channels.values():
                if channel._state in ("joined", "errored"):
                    asyncio.create_task(channel._rejoin())

        except Exception as exc:
            logger.error("Connection failed: %s", exc)
            self._connected = False
            for cb in self._on_error_callbacks:
                cb(exc)
            if self._auto_reconnect and not self._closing:
                await self._schedule_reconnect()

    async def disconnect(self) -> None:
        """Gracefully disconnect from the server."""
        self._closing = True
        self._connected = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._receive_task:
            self._receive_task.cancel()
            self._receive_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("Disconnected")

    async def _schedule_reconnect(self) -> None:
        if self._closing:
            return
        idx = min(self._reconnect_attempt, len(self._backoff) - 1)
        delay_ms = self._backoff[idx]
        self._reconnect_attempt += 1
        logger.info("Reconnecting in %dms (attempt %d)", delay_ms, self._reconnect_attempt)
        await asyncio.sleep(delay_ms / 1000)
        if not self._closing:
            await self._do_connect()

    # ─── Channel management ───────────────────────────────────

    def channel(self, topic: str, params: dict[str, Any] | None = None) -> Channel:
        """Create a channel for the given topic."""
        if topic in self._channels:
            return self._channels[topic]
        ch = Channel(self, topic, params or {})
        self._channels[topic] = ch
        return ch

    def _remove_channel(self, topic: str) -> None:
        self._channels.pop(topic, None)

    # ─── Send ─────────────────────────────────────────────────

    async def _send(
        self,
        join_ref: str | None,
        ref: str | None,
        topic: str,
        event: str,
        payload: Any,
    ) -> None:
        if not self._ws or not self._connected:
            raise ConnectionError("Socket is not connected")
        msg = json.dumps([join_ref, ref, topic, event, payload])
        await self._ws.send(msg)

    # ─── Receive loop ─────────────────────────────────────────

    async def _receive_loop(self) -> None:
        try:
            assert self._ws is not None
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON message: %s", raw[:100])
                    continue

                if not isinstance(msg, list) or len(msg) != 5:
                    logger.warning("Malformed message: %s", raw[:100])
                    continue

                join_ref, ref, topic, event, payload = msg
                self._dispatch(join_ref, ref, topic, event, payload)

        except websockets.ConnectionClosed as exc:
            logger.info("Connection closed: code=%s reason=%s", exc.code, exc.reason)
            self._connected = False
            for cb in self._on_close_callbacks:
                cb(exc.code, exc.reason)
            if self._auto_reconnect and not self._closing:
                await self._schedule_reconnect()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Receive error: %s", exc)
            self._connected = False
            for cb in self._on_error_callbacks:
                cb(exc)
            if self._auto_reconnect and not self._closing:
                await self._schedule_reconnect()

    def _dispatch(
        self,
        join_ref: str | None,
        ref: str | None,
        topic: str,
        event: str,
        payload: Any,
    ) -> None:
        # Heartbeat reply
        if ref and ref == self._pending_heartbeat_ref:
            self._pending_heartbeat_ref = None
            return

        # Route to channel
        channel = self._channels.get(topic)
        if channel:
            channel._on_message(join_ref, ref, event, payload)

    # ─── Heartbeat ────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._connected:
                    break
                if self._pending_heartbeat_ref is not None:
                    # Previous heartbeat not acknowledged — connection is dead
                    logger.warning("Heartbeat timeout — closing connection")
                    self._pending_heartbeat_ref = None
                    if self._ws:
                        await self._ws.close(1000, "heartbeat timeout")
                    break
                ref = self._make_ref()
                self._pending_heartbeat_ref = ref
                try:
                    await self._send(None, ref, "phoenix", "heartbeat", {})
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    # ─── Event callbacks ──────────────────────────────────────

    def on_open(self, callback: Callable[[], Any]) -> None:
        self._on_open_callbacks.append(callback)

    def on_close(self, callback: Callable[[int, str], Any]) -> None:
        self._on_close_callbacks.append(callback)

    def on_error(self, callback: Callable[[Exception], Any]) -> None:
        self._on_error_callbacks.append(callback)
