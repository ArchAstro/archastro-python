"""
HarnessServiceClient — Python counterpart to the TypeScript client in
``@archastro/sdk-generator/channel-harness``.

The channel-harness service exposes two surfaces that this client speaks to:

    1. A **WebSocket** endpoint carrying the real Phoenix channel protocol.
       Generated SDKs connect here to exercise their channel classes
       end-to-end.

    2. An **HTTP control** endpoint for scenario management and observation
       queries. Callers POST a JSON scenario to set up per-topic behavior,
       GET observations to assert on what the SDK actually put on the wire,
       and POST ``/reset`` between tests to drop state.

There is no in-process shortcut. Tests drive the SAME service the TypeScript
tests (or any other language) drive — the only differences are which SDK is
under test and which client library is opening the socket.
"""

from __future__ import annotations

from typing import Any

import httpx

from .socket import Socket


class HarnessServiceClient:
    """Thin client over the harness service's HTTP + WebSocket surfaces.

    One instance per test is typical::

        client = HarnessServiceClient(ws_url, control_url)
        try:
            await client.reset()
            await client.register_scenario({
                "topic": "doc:doc_42",
                "onJoin": [{"type": "autoReply"}],
            })
            socket = await client.open_socket()
            channel = await LiveDocChannel.join_document(
                socket, "doc_42", user_id="user_1"
            )
            assert channel.join_response is not None
        finally:
            await client.close()
    """

    def __init__(self, ws_url: str, control_url: str, *, request_timeout: float = 5.0):
        self.ws_url = ws_url
        self.control_url = control_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=request_timeout)
        self._sockets: list[Socket] = []

    async def close(self) -> None:
        """Disconnect every socket opened through this client + close HTTP."""
        for s in self._sockets:
            try:
                await s.disconnect()
            except Exception:
                pass
        self._sockets.clear()
        await self._http.aclose()

    # ─── HTTP control ────────────────────────────────────────────

    async def reset(self) -> None:
        """Clear every scenario, observation, and handler error on the server."""
        r = await self._http.post(f"{self.control_url}/reset")
        r.raise_for_status()

    async def register_scenario(self, scenario: dict[str, Any]) -> None:
        """Register a scenario for an exact topic.

        See the TS ``ScenarioRequest`` / ``ScenarioAction`` types for the
        JSON shape — the server validates and returns 400 on invalid bodies.
        """
        r = await self._http.post(f"{self.control_url}/scenarios", json=scenario)
        if r.status_code != 201:
            raise HarnessServiceError(f"register_scenario failed: {r.status_code} {r.text}")

    async def register_stream_scenario(self, scenario: dict[str, Any]) -> None:
        """Register a scenario for an SSE streaming route (``"METHOD /path"``).

        See the TS ``StreamScenarioRequest`` / ``StreamAction`` types for the
        JSON shape — the server validates and returns 400 on invalid bodies.
        """
        r = await self._http.post(f"{self.control_url}/stream-scenarios", json=scenario)
        if r.status_code != 201:
            raise HarnessServiceError(f"register_stream_scenario failed: {r.status_code} {r.text}")

    async def observations(
        self, topic: str | None = None, event: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch inbound frames the server validated, optionally filtered."""
        params: dict[str, str] = {}
        if topic is not None:
            params["topic"] = topic
        if event is not None:
            params["event"] = event
        r = await self._http.get(f"{self.control_url}/observations", params=params)
        r.raise_for_status()
        return r.json()

    async def handler_errors(self) -> list[dict[str, Any]]:
        """Fetch scenario handler errors recorded by the server."""
        r = await self._http.get(f"{self.control_url}/handler-errors")
        r.raise_for_status()
        return r.json()

    # ─── Socket lifecycle ────────────────────────────────────────

    async def open_socket(self, *, auto_reconnect: bool = False) -> Socket:
        """Open a fresh Phoenix socket to the service's WebSocket endpoint.

        Every call produces a new connection — the generated SDK receives
        the same ``Socket`` it would talk to in production. ``auto_reconnect``
        defaults to ``False`` so a disconnected test surfaces immediately
        rather than silently retrying in the background.
        """
        socket = Socket(self.ws_url, auto_reconnect=auto_reconnect)
        await socket.connect()
        self._sockets.append(socket)
        return socket


class HarnessServiceError(RuntimeError):
    """Raised when the harness service returns a non-success HTTP status."""
