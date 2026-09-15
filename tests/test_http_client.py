# Copyright (c) 2026 ArchAstro Inc. All Rights Reserved.
"""Unit tests for HttpClient 401 auto-refresh — mirrors the TS test suite."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from archastro.platform.runtime.http_client import (
    ApiError,
    HttpClient,
    SyncHttpClient,
    request_timeout,
)


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
        timeout=httpx.USE_CLIENT_DEFAULT,
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


# ─── response_type deserialization ──────────────────────────────


class _Widget(BaseModel):
    id: str
    count: int


async def test_deserializes_into_response_type_model():
    client = HttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"id": "w_1", "count": 3}),
    ):
        result = await client.request("/api/v1/widgets/w_1", response_type=_Widget)

    assert isinstance(result, _Widget)
    assert result.id == "w_1"
    assert result.count == 3


async def test_deserializes_into_list_of_response_type_models():
    client = HttpClient(base_url="https://api.test")
    body = [{"id": "w_1", "count": 1}, {"id": "w_2", "count": 2}]
    resp = httpx.Response(
        status_code=200, json=body, request=httpx.Request("GET", "https://api.test")
    )

    with patch.object(client._client, "request", return_value=resp):
        result = await client.request("/api/v1/widgets", response_type=list[_Widget])

    assert isinstance(result, list)
    assert all(isinstance(item, _Widget) for item in result)
    assert [item.id for item in result] == ["w_1", "w_2"]


async def test_returns_raw_dict_without_response_type():
    client = HttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"id": "w_1", "count": 3}),
    ):
        result = await client.request("/api/v1/widgets/w_1")

    assert result == {"id": "w_1", "count": 3}
    assert not isinstance(result, BaseModel)


async def test_204_returns_none_without_response_type():
    client = HttpClient(base_url="https://api.test")
    resp = httpx.Response(status_code=204, request=httpx.Request("DELETE", "https://api.test"))

    with patch.object(client._client, "request", return_value=resp):
        result = await client.request("/api/v1/widgets/w_1", method="DELETE")

    assert result is None


async def test_204_with_response_type_raises_validation_error():
    # A bodyless 204 on an operation that promises a typed body is a server
    # contract violation and should fail at the call, not as a downstream
    # AttributeError on None.
    client = HttpClient(base_url="https://api.test")
    resp = httpx.Response(status_code=204, request=httpx.Request("DELETE", "https://api.test"))

    with patch.object(client._client, "request", return_value=resp):
        with pytest.raises(ValidationError):
            await client.request("/api/v1/widgets/w_1", method="DELETE", response_type=_Widget)


async def test_invalid_payload_raises_validation_error():
    client = HttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"id": "w_1", "count": "not-a-number"}),
    ):
        with pytest.raises(ValidationError):
            await client.request("/api/v1/widgets/w_1", response_type=_Widget)


def test_sync_deserializes_into_response_type_model():
    client = SyncHttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"id": "w_1", "count": 3}),
    ):
        result = client.request("/api/v1/widgets/w_1", response_type=_Widget)

    assert isinstance(result, _Widget)
    assert result.id == "w_1"
    assert result.count == 3


def test_sync_deserializes_into_list_of_response_type_models():
    client = SyncHttpClient(base_url="https://api.test")
    body = [{"id": "w_1", "count": 1}, {"id": "w_2", "count": 2}]
    resp = httpx.Response(
        status_code=200, json=body, request=httpx.Request("GET", "https://api.test")
    )

    with patch.object(client._client, "request", return_value=resp):
        result = client.request("/api/v1/widgets", response_type=list[_Widget])

    assert isinstance(result, list)
    assert all(isinstance(item, _Widget) for item in result)


def test_sync_returns_raw_dict_without_response_type():
    client = SyncHttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"id": "w_1", "count": 3}),
    ):
        result = client.request("/api/v1/widgets/w_1")

    assert result == {"id": "w_1", "count": 3}
    assert not isinstance(result, BaseModel)


def test_sync_204_returns_none_without_response_type():
    client = SyncHttpClient(base_url="https://api.test")
    resp = httpx.Response(status_code=204, request=httpx.Request("DELETE", "https://api.test"))

    with patch.object(client._client, "request", return_value=resp):
        result = client.request("/api/v1/widgets/w_1", method="DELETE")

    assert result is None


def test_sync_204_with_response_type_raises_validation_error():
    client = SyncHttpClient(base_url="https://api.test")
    resp = httpx.Response(status_code=204, request=httpx.Request("DELETE", "https://api.test"))

    with patch.object(client._client, "request", return_value=resp):
        with pytest.raises(ValidationError):
            client.request("/api/v1/widgets/w_1", method="DELETE", response_type=_Widget)


def test_sync_invalid_payload_raises_validation_error():
    client = SyncHttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {"id": "w_1", "count": "not-a-number"}),
    ):
        with pytest.raises(ValidationError):
            client.request("/api/v1/widgets/w_1", response_type=_Widget)


# ─── request assembly, token sources, and error parsing ────────


async def test_async_client_sends_auth_headers_query_and_json_body():
    client = HttpClient(
        base_url="https://api.test",
        access_token="sat_test",
        path_prefix="/proxy/v1",
        default_headers={"x-archastro-api-key": "pk_test"},
    )

    with patch.object(
        client._client,
        "request",
        new=AsyncMock(return_value=_mock_response(200, {"ok": True})),
    ) as request:
        result = await client.request(
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
        timeout=httpx.USE_CLIENT_DEFAULT,
    )


async def test_async_client_drops_body_and_keeps_path_for_get_outside_api_prefix():
    client = HttpClient(base_url="https://api.test", path_prefix="/proxy/v1")

    with patch.object(
        client._client,
        "request",
        new=AsyncMock(return_value=_mock_response(200, {})),
    ) as request:
        await client.request("/health", body={"ignored": True})

    request.assert_called_once_with(
        "GET",
        "https://api.test/health",
        json=None,
        headers={"Content-Type": "application/json"},
        params=None,
        timeout=httpx.USE_CLIENT_DEFAULT,
    )


async def test_get_access_token_callable_takes_precedence_over_static_token():
    client = HttpClient(
        base_url="https://api.test",
        access_token="static-token",
        get_access_token=lambda: "dynamic-token",
    )

    with patch.object(
        client._client,
        "request",
        new=AsyncMock(return_value=_mock_response(200, {})),
    ) as request:
        await client.request("/api/v1/things")

    headers = request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer dynamic-token"


def test_sync_get_access_token_callable_takes_precedence_over_static_token():
    client = SyncHttpClient(
        base_url="https://api.test",
        access_token="static-token",
        get_access_token=lambda: "dynamic-token",
    )

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(200, {}),
    ) as request:
        client.request("/api/v1/things")

    headers = request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer dynamic-token"


async def test_async_client_request_raw_returns_bytes_and_mime_type():
    client = HttpClient(base_url="https://api.test")
    response = httpx.Response(
        status_code=200,
        content=b"hello",
        headers={"content-type": "text/plain"},
        request=httpx.Request("GET", "https://api.test"),
    )

    with patch.object(client._client, "request", new=AsyncMock(return_value=response)):
        result = await client.request_raw("/api/v1/files/file_123/download")

    assert result == {"content": b"hello", "mime_type": "text/plain"}


def test_sync_client_throws_original_401_when_refresh_handler_fails():
    def failing_handler() -> str:
        raise RuntimeError("refresh token expired")

    client = SyncHttpClient(
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
            client.request("/api/v1/things")

    assert exc_info.value.status == 401


def test_error_code_falls_back_to_type_when_code_missing():
    client = SyncHttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(
            422, {"error": {"type": "invalid_request", "message": "bad input"}}
        ),
    ):
        with pytest.raises(ApiError) as exc_info:
            client.request("/api/v1/things")

    assert exc_info.value.error_code == "invalid_request"
    assert str(exc_info.value) == "bad input"


def test_string_error_body_used_as_code_and_message():
    client = SyncHttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(400, {"error": "bad_thing"}),
    ):
        with pytest.raises(ApiError) as exc_info:
            client.request("/api/v1/things")

    assert exc_info.value.error_code == "bad_thing"
    assert str(exc_info.value) == "bad_thing"


def test_message_only_error_body_keeps_unknown_code():
    client = SyncHttpClient(base_url="https://api.test")

    with patch.object(
        client._client,
        "request",
        return_value=_mock_response(400, {"message": "boom"}),
    ):
        with pytest.raises(ApiError) as exc_info:
            client.request("/api/v1/things")

    assert exc_info.value.error_code == "unknown_error"
    assert str(exc_info.value) == "boom"


def test_non_json_error_body_falls_back_to_http_status_message():
    client = SyncHttpClient(base_url="https://api.test")
    response = httpx.Response(
        status_code=500,
        content=b"<html>Internal Server Error</html>",
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", "https://api.test"),
    )

    with patch.object(client._client, "request", return_value=response):
        with pytest.raises(ApiError) as exc_info:
            client.request("/api/v1/things")

    assert exc_info.value.error_code == "unknown_error"
    assert str(exc_info.value) == "HTTP 500"


# ─── SSE streaming (stream_sse / stream_sse_sync) ──────────────────


class _FakeAsyncResponse:
    def __init__(self, status_code, lines, json_body=None):
        self.status_code = status_code
        self._lines = lines
        self._json = json_body or {}

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    def json(self):
        return self._json


class _FakeAsyncStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeSyncResponse:
    def __init__(self, status_code, lines, json_body=None):
        self.status_code = status_code
        self._lines = lines
        self._json = json_body or {}

    def iter_lines(self):
        yield from self._lines

    def read(self):
        return b""

    def json(self):
        return self._json


class _FakeSyncStream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


_SSE_LINES = [
    "event: chunk",
    'data: {"text": "He"}',
    "",
    "event: chunk",
    'data: {"text": "llo"}',
    "",
    "event: done",
    'data: {"ok": true}',
    "",
]


async def test_stream_sse_yields_parsed_events_and_sends_body():
    client = HttpClient(base_url="https://api.test")
    resp = _FakeAsyncResponse(200, _SSE_LINES)
    with patch.object(client._client, "stream", return_value=_FakeAsyncStream(resp)) as m:
        events = [
            ev
            async for ev in client.stream_sse(
                "/api/v1/echo/stream", method="POST", body={"prompt": "hi"}
            )
        ]

    assert events == [
        {"event": "chunk", "data": {"text": "He"}},
        {"event": "chunk", "data": {"text": "llo"}},
        {"event": "done", "data": {"ok": True}},
    ]
    _, kwargs = m.call_args
    assert kwargs["json"] == {"prompt": "hi"}
    assert kwargs["headers"]["Accept"] == "text/event-stream"


async def test_stream_sse_raises_apierror_on_non_2xx():
    client = HttpClient(base_url="https://api.test")
    resp = _FakeAsyncResponse(
        402, [], json_body={"error": {"code": "plan_not_entitled", "message": "no"}}
    )
    with patch.object(client._client, "stream", return_value=_FakeAsyncStream(resp)):
        with pytest.raises(ApiError) as exc_info:
            [ev async for ev in client.stream_sse("/api/v1/x/stream", method="POST", body={})]
    assert exc_info.value.status == 402


def test_stream_sse_sync_yields_parsed_events():
    client = SyncHttpClient(base_url="https://api.test")
    resp = _FakeSyncResponse(200, _SSE_LINES)
    with patch.object(client._client, "stream", return_value=_FakeSyncStream(resp)):
        events = list(
            client.stream_sse_sync("/api/v1/echo/stream", method="POST", body={"prompt": "hi"})
        )
    assert events == [
        {"event": "chunk", "data": {"text": "He"}},
        {"event": "chunk", "data": {"text": "llo"}},
        {"event": "done", "data": {"ok": True}},
    ]


def test_stream_sse_sync_raises_apierror_on_non_2xx():
    client = SyncHttpClient(base_url="https://api.test")
    resp = _FakeSyncResponse(401, [], json_body={"error": "unauthenticated"})
    with patch.object(client._client, "stream", return_value=_FakeSyncStream(resp)):
        with pytest.raises(ApiError):
            list(client.stream_sse_sync("/api/v1/x/stream", method="POST", body={}))


# --- query encoding contract -------------------------------------------------
#
# The platform reads query parameters through Plug, whose parser keeps only the
# last value of a repeated bare key. A multi-value filter sent as
# `source=a&source=b` therefore silently narrows to one value server-side. These
# tests pin the wire bytes rather than the dict handed to httpx, because the
# defect only becomes visible after httpx encodes.


async def test_async_list_query_params_encode_as_bracket_suffixed_repeats():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = HttpClient(base_url="https://api.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await client.request(
        "/api/v1/knowledge_documents",
        query={"source": ["cso_a", "cso_b", "cso_c"], "page_size": 100},
    )

    assert seen[0].url.query == (
        b"source%5B%5D=cso_a&source%5B%5D=cso_b&source%5B%5D=cso_c&page_size=100"
    )
    # Every element survives the round trip as a distinct value under one key.
    assert seen[0].url.params.get_list("source[]") == ["cso_a", "cso_b", "cso_c"]


async def test_async_scalar_query_params_are_unchanged_and_none_is_dropped():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = HttpClient(base_url="https://api.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await client.request(
        "/api/v1/knowledge_documents",
        query={"q": "notes", "page_size": 25, "agent": None},
    )

    assert seen[0].url.query == b"q=notes&page_size=25"


def test_sync_list_query_params_encode_as_bracket_suffixed_repeats():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = SyncHttpClient(base_url="https://api.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))

    client.request(
        "/api/v1/knowledge_documents",
        query={"source": ["cso_a", "cso_b", "cso_c"], "page_size": 100},
    )

    assert seen[0].url.query == (
        b"source%5B%5D=cso_a&source%5B%5D=cso_b&source%5B%5D=cso_c&page_size=100"
    )


def test_sync_scalar_query_params_are_unchanged_and_none_is_dropped():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = SyncHttpClient(base_url="https://api.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))

    client.request(
        "/api/v1/knowledge_documents",
        query={"q": "notes", "page_size": 25, "agent": None},
    )

    assert seen[0].url.query == b"q=notes&page_size=25"


# --- request timeout contract ------------------------------------------------
#
# Callers that run under an overall budget (the benchmark harness passes each
# platform call the *remaining* seconds of its step) need every request to
# honor a timeout they choose, including requests issued by generated resource
# methods that expose no timeout argument. These tests read the timeout httpx
# actually attaches to the outgoing request, not the arguments handed to it.


def _timeouts(request: httpx.Request) -> dict[str, float]:
    return request.extensions["timeout"]


def _all(seconds: float) -> dict[str, float]:
    return {"connect": seconds, "read": seconds, "write": seconds, "pool": seconds}


def _recording_sync_client(**kwargs) -> tuple[SyncHttpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = SyncHttpClient(base_url="https://api.test", **kwargs)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=client._timeout)
    return client, seen


def _recording_async_client(**kwargs) -> tuple[HttpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = HttpClient(base_url="https://api.test", **kwargs)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=client._timeout
    )
    return client, seen


def test_sync_default_request_timeout_is_thirty_seconds():
    client, seen = _recording_sync_client()

    client.request("/api/v1/things")

    assert _timeouts(seen[0]) == _all(30.0)


def test_sync_constructor_timeout_sets_the_client_default():
    client = SyncHttpClient(base_url="https://api.test", timeout=7.5)

    assert client._client.timeout == httpx.Timeout(7.5)


async def test_async_constructor_timeout_sets_the_client_default():
    client = HttpClient(base_url="https://api.test", timeout=7.5)

    assert client._client.timeout == httpx.Timeout(7.5)


def test_sync_request_timeout_overrides_only_inside_its_block():
    client, seen = _recording_sync_client(timeout=30.0)

    with request_timeout(4.25):
        client.request("/api/v1/things")
        client.request_raw("/api/v1/things/content")
    client.request("/api/v1/things")

    assert [_timeouts(r) for r in seen] == [_all(4.25), _all(4.25), _all(30.0)]


async def test_async_request_timeout_overrides_only_inside_its_block():
    client, seen = _recording_async_client(timeout=30.0)

    with request_timeout(4.25):
        await client.request("/api/v1/things")
        await client.request_raw("/api/v1/things/content")
    await client.request("/api/v1/things")

    assert [_timeouts(r) for r in seen] == [_all(4.25), _all(4.25), _all(30.0)]


def test_nested_request_timeout_innermost_wins_then_restores():
    client, seen = _recording_sync_client()

    with request_timeout(10.0):
        with request_timeout(2.0):
            client.request("/api/v1/inner")
        client.request("/api/v1/outer")

    assert [_timeouts(r) for r in seen] == [_all(2.0), _all(10.0)]


def test_request_timeout_retry_after_401_uses_the_same_override():
    responses = iter([httpx.Response(401, json={}), httpx.Response(200, json={"ok": True})])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(responses)

    client = SyncHttpClient(
        base_url="https://api.test",
        access_token="expired",
        on_refresh_token=lambda: "fresh",
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))

    with request_timeout(3.0):
        assert client.request("/api/v1/things") == {"ok": True}

    assert [_timeouts(r) for r in seen] == [_all(3.0), _all(3.0)]


def test_request_timeout_does_not_leak_into_other_threads():
    import threading

    client, seen = _recording_sync_client(timeout=30.0)
    entered = threading.Event()
    release = threading.Event()

    def hold_override():
        with request_timeout(1.0):
            entered.set()
            release.wait(5)

    holder = threading.Thread(target=hold_override)
    holder.start()
    try:
        assert entered.wait(5)
        client.request("/api/v1/things")
    finally:
        release.set()
        holder.join()

    assert _timeouts(seen[0]) == _all(30.0)


@pytest.mark.parametrize("budget", [0.0, -0.5, float("nan")])
def test_sync_expired_budget_raises_timeout_error_without_sending(budget):
    client, seen = _recording_sync_client()

    with request_timeout(budget):
        with pytest.raises(TimeoutError, match="GET /api/v1/things"):
            client.request("/api/v1/things")
        with pytest.raises(TimeoutError):
            client.request_raw("/api/v1/things")
        with pytest.raises(TimeoutError):
            list(client.stream_sse_sync("/api/v1/things/stream"))

    assert seen == []


@pytest.mark.parametrize("budget", [0.0, -0.5, float("nan")])
async def test_async_expired_budget_raises_timeout_error_without_sending(budget):
    client, seen = _recording_async_client()

    with request_timeout(budget):
        with pytest.raises(TimeoutError, match="POST /api/v1/things"):
            await client.request("/api/v1/things", method="POST", body={})
        with pytest.raises(TimeoutError):
            await client.request_raw("/api/v1/things")
        with pytest.raises(TimeoutError):
            [ev async for ev in client.stream_sse("/api/v1/things/stream")]

    assert seen == []


def test_sync_stream_honors_request_timeout():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b'event: done\ndata: {"ok": true}\n\n')

    client = SyncHttpClient(base_url="https://api.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))

    with request_timeout(6.0):
        events = list(client.stream_sse_sync("/api/v1/echo/stream"))

    assert events == [{"event": "done", "data": {"ok": True}}]
    assert _timeouts(seen[0]) == _all(6.0)


async def test_async_stream_honors_request_timeout():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b'event: done\ndata: {"ok": true}\n\n')

    client = HttpClient(base_url="https://api.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with request_timeout(6.0):
        events = [ev async for ev in client.stream_sse("/api/v1/echo/stream")]

    assert events == [{"event": "done", "data": {"ok": True}}]
    assert _timeouts(seen[0]) == _all(6.0)


def test_generated_resource_methods_honor_request_timeout():
    from archastro.platform.client import PlatformClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [],
                "has_next": False,
                "has_prev": False,
                "page": 1,
                "page_size": 10,
                "total_entries": 0,
                "total_pages": 0,
            },
        )

    client = PlatformClient.with_secret_key("sk_test", base_url="https://api.test")
    client._http._client = httpx.Client(transport=httpx.MockTransport(handler))

    with request_timeout(2.5):
        client.knowledge_documents.list(source=["cso_a"], page_size=10)

    assert _timeouts(seen[0]) == _all(2.5)
