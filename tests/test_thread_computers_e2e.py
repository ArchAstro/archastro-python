# Copyright (c) 2026 ArchAstro Inc. All Rights Reserved.
"""Thread computers over real loopback HTTP.

CI runs this file from the built wheel ("Build and import wheel" step), so it
exercises the package as published. The canned server records the wire
contract only; it does not model platform authorization or provisioning.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from archastro.platform import PlatformClient

COMPUTER = {
    "id": "cmp_thread_1",
    "name": "Build box",
    "thread_id": "thr_1",
    "status": "provisioning",
    "provider": "test",
    "region": "local",
    "config": {},
    "metadata": {"purpose": "e2e"},
    "created_at": "2026-10-21T00:00:00Z",
    "updated_at": "2026-10-21T00:00:00Z",
}

CANNED: dict[str, tuple[int, Any]] = {
    "POST /api/v1/threads/thr_1/computers": (201, COMPUTER),
    "GET /api/v1/threads/thr_1/computers": (200, {"data": [COMPUTER]}),
    "PATCH /api/v1/agent_computers/cmp_thread_1": (204, None),
}


class _Platform:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.base_url = ""


@pytest.fixture
def platform() -> Iterator[_Platform]:
    state = _Platform()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            state.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "api_key": self.headers.get("x-archastro-api-key"),
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": json.loads(raw) if raw else None,
                }
            )
            status, body = CANNED.get(f"{self.command} {self.path}", (404, {"error": "not_found"}))
            self.send_response(status)
            if body is None:
                self.end_headers()
                return
            payload = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = _handle

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_published_sdk_creates_and_lists_thread_computers_and_updates_computer_shares(
    platform: _Platform,
) -> None:
    # Setup: an API-key + bearer client pointed at the loopback platform.
    client = PlatformClient.with_token("pk_test_key", "user_access_token", platform.base_url)

    # Action: provision, list, then revoke every direct share with empty grants.
    created = client.threads.computers.create(
        "thr_1", {"name": "Build box", "metadata": {"purpose": "e2e"}}
    )
    listed = client.threads.computers.list("thr_1")
    updated = client.agent_computers.update("cmp_thread_1", {"acl": {"grants": []}})

    # Outcome: the SDK decoded each response body into typed results (None on 204)...
    assert created.id == "cmp_thread_1"
    assert created.thread_id == "thr_1"
    assert created.metadata == {"purpose": "e2e"}
    assert [computer.id for computer in listed.data] == ["cmp_thread_1"]
    assert updated is None

    # ...and sent the documented wire contract.
    assert [(r["method"], r["path"], r["body"]) for r in platform.requests] == [
        (
            "POST",
            "/api/v1/threads/thr_1/computers",
            {"name": "Build box", "metadata": {"purpose": "e2e"}},
        ),
        ("GET", "/api/v1/threads/thr_1/computers", None),
        ("PATCH", "/api/v1/agent_computers/cmp_thread_1", {"acl": {"grants": []}}),
    ]
    for request in platform.requests:
        assert request["api_key"] == "pk_test_key"
        assert request["authorization"] == "Bearer user_access_token"
        assert request["content_type"] == "application/json"
