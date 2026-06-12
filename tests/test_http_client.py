# Copyright (c) 2026 ArchAstro Inc. All Rights Reserved.
"""Unit tests for HttpClient 401 auto-refresh — mirrors the TS test suite."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from archastro.platform.runtime.http_client import ApiError, HttpClient, SyncHttpClient


def _mock_response(status: int, body: dict | None = None) -> httpx.Response:
    """Build a fake httpx.Response with the given status and JSON body."""
    resp = httpx.Response(
        status_code=status,
        json=body or {},
        request=httpx.Request("GET", "https://api.test"),
    )
    return resp


async def test_retries_with_new_token_after_401():
    client = HttpClient(
        base_url="https://api.test",
        access_token="expired-token",
        on_refresh_token=AsyncMock(return_value="fresh-token"),
    )
    responses = [
        _mock_response(401, {"error": "unauthenticated", "message": "expired"}),
        _mock_response(200, {"id": "123"}),
    ]
    call_count = 0

    async def mock_request(*args, **kwargs):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    with patch.object(client._client, "request", side_effect=mock_request):
        result = await client.request("/api/v1/things")

    assert result == {"id": "123"}
    assert call_count == 2
    # Second call should use the new token
    client._on_refresh_token.assert_called_once()


async def test_throws_401_when_no_refresh_handler():
    client = HttpClient(base_url="https://api.test", access_token="expired-token")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(401, {"error": "unauthenticated"}),
    ):
        with pytest.raises(ApiError) as exc_info:
            await client.request("/api/v1/things")
    assert exc_info.value.status == 401


async def test_throws_401_when_refresh_handler_fails():
    async def failing_handler():
        raise RuntimeError("refresh token expired")

    client = HttpClient(
        base_url="https://api.test",
        access_token="expired-token",
        on_refresh_token=failing_handler,
    )

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(401, {"error": "unauthenticated"}),
    ):
        with pytest.raises(ApiError) as exc_info:
            await client.request("/api/v1/things")
    assert exc_info.value.status == 401


async def test_does_not_retry_on_non_401_errors():
    handler = AsyncMock(return_value="fresh-token")
    client = HttpClient(
        base_url="https://api.test",
        access_token="some-token",
        on_refresh_token=handler,
    )

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(403, {"error": "forbidden"}),
    ):
        with pytest.raises(ApiError) as exc_info:
            await client.request("/api/v1/things")
    assert exc_info.value.status == 403
    handler.assert_not_called()


async def test_does_not_retry_auth_paths():
    handler = AsyncMock(return_value="fresh-token")
    client = HttpClient(
        base_url="https://api.test",
        access_token="expired-token",
        on_refresh_token=handler,
    )

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(401, {"error": "unauthenticated"}),
    ):
        with pytest.raises(ApiError):
            await client.request("/api/v1/auth/refresh", method="POST")
    handler.assert_not_called()


async def test_refresh_only_client_throws_on_non_auth_paths():
    client = HttpClient(base_url="https://api.test", refresh_only=True)

    with pytest.raises(RuntimeError, match="Refresh-only HTTP client"):
        await client.request("/api/v1/agents")

    # Auth paths should work
    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"token": "t"}),
    ):
        result = await client.request("/api/v1/auth/refresh", method="POST")
    assert result == {"token": "t"}


async def test_concurrent_401s_piggyback_on_same_refresh():
    refresh_call_count = 0

    async def refresh_handler():
        nonlocal refresh_call_count
        refresh_call_count += 1
        return "fresh-token"

    client = HttpClient(
        base_url="https://api.test",
        access_token="expired-token",
        on_refresh_token=refresh_handler,
    )

    call_count = 0

    async def mock_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return _mock_response(401, {"error": "unauthenticated"})
        return _mock_response(200, {"id": f"item-{call_count}"})

    with patch.object(client._client, "request", side_effect=mock_request):
        import asyncio

        a, b = await asyncio.gather(
            client.request("/api/v1/things"),
            client.request("/api/v1/stuff"),
        )

    assert a["id"]
    assert b["id"]
    assert refresh_call_count == 1
    assert call_count == 4  # 2 original (401) + 2 retries (200)


async def test_set_refresh_handler_wires_handler_post_construction():
    client = HttpClient(base_url="https://api.test", access_token="expired-token")
    client.set_refresh_handler(AsyncMock(return_value="refreshed-token"))

    responses = [
        _mock_response(401, {"error": "unauthenticated"}),
        _mock_response(200, {"ok": True}),
    ]
    call_count = 0

    async def mock_request(*args, **kwargs):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    with patch.object(client._client, "request", side_effect=mock_request):
        result = await client.request("/api/v1/things")

    assert result == {"ok": True}
    assert call_count == 2


async def test_propagates_retry_error_when_refresh_succeeds_but_retry_fails():
    client = HttpClient(
        base_url="https://api.test",
        access_token="expired-token",
        on_refresh_token=AsyncMock(return_value="fresh-token"),
    )

    responses = [
        _mock_response(401, {"error": "unauthenticated"}),
        _mock_response(403, {"error": "forbidden", "message": "no access"}),
    ]
    call_count = 0

    async def mock_request(*args, **kwargs):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    with patch.object(client._client, "request", side_effect=mock_request):
        with pytest.raises(ApiError) as exc_info:
            await client.request("/api/v1/things")
    assert exc_info.value.status == 403
    assert call_count == 2


async def test_clears_refresh_task_after_failure_so_future_refreshes_work():
    call_count = 0

    async def mock_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return _mock_response(401, {"error": "unauthenticated"})
        return _mock_response(200, {"id": "ok"})

    attempt = 0

    async def refresh_handler():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise RuntimeError("refresh failed")
        return "fresh-token"

    client = HttpClient(
        base_url="https://api.test",
        access_token="expired-token",
        on_refresh_token=refresh_handler,
    )

    with patch.object(client._client, "request", side_effect=mock_request):
        # First call: refresh fails → throws 401
        with pytest.raises(ApiError):
            await client.request("/api/v1/things")

        # Second call: refresh succeeds → retries → 200
        result = await client.request("/api/v1/things")

    assert result == {"id": "ok"}
    assert attempt == 2


def test_sync_client_sends_auth_headers_query_and_json_body():
    client = SyncHttpClient(
        base_url="https://api.test",
        access_token="sat_test",
        path_prefix="/proxy/v1",
        default_headers={"x-archastro-api-key": "pk_test"},
    )

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"ok": True}),
    ) as request:
        result = client.request(
            "/api/v1/things",
            method="POST",
            body={"name": "demo"},
            query={"limit": 10, "empty": None},
        )

    assert result == {"ok": True}
    request.assert_called_once_with(
        "POST",
        "https://api.test/proxy/v1/things",
        json={"name": "demo"},
        headers={
            "x-archastro-api-key": "pk_test",
            "Content-Type": "application/json",
            "Authorization": "Bearer sat_test",
        },
        params={"limit": 10},
    )


def test_sync_client_retries_with_new_token_after_401():
    refresh_handler = Mock(return_value="fresh-token")
    client = SyncHttpClient(
        base_url="https://api.test",
        access_token="expired-token",
        on_refresh_token=refresh_handler,
    )
    responses = [
        _mock_response(401, {"error": "unauthenticated", "message": "expired"}),
        _mock_response(200, {"id": "123"}),
    ]

    with patch.object(client._client, "request", side_effect=responses) as request:
        result = client.request("/api/v1/things")

    assert result == {"id": "123"}
    assert request.call_count == 2
    refresh_handler.assert_called_once_with()
    assert request.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer fresh-token"


def test_sync_client_does_not_retry_auth_paths():
    refresh_handler = Mock(return_value="fresh-token")
    client = SyncHttpClient(
        base_url="https://api.test",
        access_token="expired-token",
        on_refresh_token=refresh_handler,
    )

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(401, {"error": "unauthenticated"}),
    ):
        with pytest.raises(ApiError) as exc_info:
            client.request("/api/v1/auth/refresh", method="POST")

    assert exc_info.value.status == 401
    refresh_handler.assert_not_called()


def test_sync_refresh_only_client_throws_on_non_auth_paths():
    client = SyncHttpClient(base_url="https://api.test", refresh_only=True)

    with pytest.raises(RuntimeError, match="Refresh-only HTTP client"):
        client.request("/api/v1/agents")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"token": "t"}),
    ):
        result = client.request("/api/v1/auth/refresh", method="POST")

    assert result == {"token": "t"}


def test_sync_client_request_raw_returns_bytes_and_mime_type():
    client = SyncHttpClient(base_url="https://api.test")
    response = httpx.Response(
        status_code=200,
        content=b"hello",
        headers={"content-type": "text/plain"},
        request=httpx.Request("GET", "https://api.test"),
    )

    with patch.object(client._client, "request", return_value=response):
        result = client.request_raw("/api/v1/files/file_123/download")

    assert result == {"content": b"hello", "mime_type": "text/plain"}


def test_sync_client_raises_structured_api_error():
    client = SyncHttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(
            403,
            {"error": {"code": "forbidden", "message": "no access"}},
        ),
    ):
        with pytest.raises(ApiError) as exc_info:
            client.request("/api/v1/things")

    assert exc_info.value.status == 403
    assert exc_info.value.error_code == "forbidden"
    assert str(exc_info.value) == "no access"
