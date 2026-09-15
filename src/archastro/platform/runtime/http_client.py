# Runtime: async HTTP client for the generated Platform SDK.
# This file is hand-maintained, not generated.

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from typing import Any, TypeVar, overload

import httpx
from pydantic import TypeAdapter

DEFAULT_API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT_S = 30.0

T = TypeVar("T")

_request_timeout: ContextVar[float | None] = ContextVar("archastro_request_timeout", default=None)


@contextmanager
def request_timeout(seconds: float) -> Iterator[None]:
    """Override the timeout of every request issued inside this block.

    Applies to all SDK clients in the current context (a thread, or an
    asyncio task and anything that copies its context such as
    ``asyncio.to_thread``), including requests made by generated resource
    methods, which take no timeout argument of their own. The value is read
    when a request is sent; a stream reads it when iteration starts, so start
    reading inside the block. It is the httpx timeout for each HTTP request (a
    refresh-and-retry after a 401 gets the same value), not a total for the
    block. Blocks nest; the innermost wins.

    A value that is not positive (including NaN) raises :class:`TimeoutError`
    before any request is sent, so a caller passing down the remainder of an
    overall deadline fails fast once that deadline has passed.
    """
    token = _request_timeout.set(float(seconds))
    try:
        yield
    finally:
        _request_timeout.reset(token)


def _resolve_timeout(method: str, path: str) -> Any:
    """The per-request timeout override, or httpx's client-default sentinel."""
    seconds = _request_timeout.get()
    if seconds is None:
        return httpx.USE_CLIENT_DEFAULT
    if not seconds > 0:
        raise TimeoutError(
            f"request timeout budget exhausted before {method} {path} (timeout={seconds:.3f}s)"
        )
    return seconds


def _encode_query(query: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop unset parameters and encode list values as ``key[]=item``.

    httpx writes a list under a bare repeated key (``source=a&source=b``),
    and Plug's query parser keeps only the last value — the server would
    silently filter on one element of a multi-value filter. The bracket
    suffix parses as a list and matches the TypeScript SDK's encoding.
    """
    if not query:
        return None
    params: dict[str, Any] = {}
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            params[f"{key}[]"] = [str(item) for item in value]
        else:
            params[key] = value
    return params or None


@cache
def _type_adapter(tp: Any) -> TypeAdapter[Any]:
    """Cache adapters per response type; TypeAdapter construction is costly."""
    return TypeAdapter(tp)


class ApiError(Exception):
    """Structured API error with status code, error code, and message."""

    def __init__(
        self,
        status: int,
        error_code: str,
        message: str,
        body: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.body = body


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str | None = None,
        get_access_token: Callable[[], str | None] | None = None,
        on_refresh_token: Callable[[], Coroutine[Any, Any, str]] | None = None,
        path_prefix: str | None = None,
        default_headers: dict[str, str] | None = None,
        refresh_only: bool = False,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._get_access_token = get_access_token
        self._on_refresh_token = on_refresh_token
        self._path_prefix = path_prefix
        self._default_headers = default_headers or {}
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        self._refresh_task: asyncio.Task[str] | None = None
        self._refresh_only = refresh_only

    def _get_token(self) -> str | None:
        if self._get_access_token:
            return self._get_access_token()
        return self._access_token

    def _transform_path(self, path: str) -> str:
        if self._path_prefix is None:
            return path
        if path.startswith(DEFAULT_API_PREFIX):
            return self._path_prefix + path[len(DEFAULT_API_PREFIX) :]
        return path

    def set_access_token(self, token: str) -> None:
        self._access_token = token

    def set_refresh_handler(self, handler: Callable[[], Coroutine[Any, Any, str]]) -> None:
        self._on_refresh_token = handler

    async def _do_fetch(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> httpx.Response:
        timeout = _resolve_timeout(method, path)
        token = self._get_token()
        url = f"{self._base_url}{self._transform_path(path)}"

        req_headers = {
            **self._default_headers,
            "Content-Type": "application/json",
        }
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        if headers:
            req_headers.update(headers)

        params = _encode_query(query)

        return await self._client.request(
            method,
            url,
            json=body if body is not None and method not in ("GET", "HEAD") else None,
            headers=req_headers,
            params=params,
            timeout=timeout,
        )

    async def _execute(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Fetch with auth gate, 401 auto-refresh, and error handling.

        Returns the successful response for callers to interpret (JSON, raw bytes, etc.).
        """
        auth_prefix = f"{DEFAULT_API_PREFIX}/auth/"
        if self._refresh_only and not path.startswith(auth_prefix):
            raise RuntimeError(
                f"Refresh-only HTTP client cannot make requests outside {auth_prefix}"
            )

        response = await self._do_fetch(
            path, method=method, body=body, headers=headers, query=query
        )

        # Auto-refresh: on 401, attempt one token refresh and retry.
        # The refresh handler runs on a separate HttpClient (refresh_only),
        # so it cannot re-enter this block. Concurrent 401s piggyback on
        # the same _refresh_task.
        if (
            response.status_code == 401
            and self._on_refresh_token
            and not path.startswith(auth_prefix)
        ):
            if self._refresh_task is None:

                async def _do_refresh() -> str:
                    try:
                        return await self._on_refresh_token()  # type: ignore[misc]
                    finally:
                        self._refresh_task = None

                self._refresh_task = asyncio.create_task(_do_refresh())
            try:
                new_token = await self._refresh_task
            except Exception:
                pass  # refresh failed — fall through to throw original 401
            else:
                self._access_token = new_token
                response = await self._do_fetch(
                    path, method=method, body=body, headers=headers, query=query
                )

        if response.status_code >= 400:
            raw_data: dict[str, Any] = {}
            try:
                raw_data = response.json()
            except Exception:
                pass
            error_code, message = _parse_error(raw_data, response.status_code)
            raise ApiError(response.status_code, error_code, message, raw_data)

        return response

    @overload
    async def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        response_type: type[T],
    ) -> T: ...

    @overload
    async def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        response_type: None = None,
    ) -> Any: ...

    async def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        response_type: type[T] | None = None,
    ) -> T | Any:
        """Issue a request and return the JSON body.

        With `response_type`, the body is validated into that type (a Pydantic
        model or a generic alias like list[Model]); a bodyless 204 then raises
        ValidationError, since the operation promised a typed body. Without
        `response_type`, the raw parsed JSON is returned, or None on 204.
        """
        response = await self._execute(path, method=method, body=body, headers=headers, query=query)

        if response.status_code == 204:
            if response_type is None:
                return None
            # A 204 on an operation that promises a typed body is a server
            # contract violation; validating None fails loudly here instead
            # of surfacing later as an AttributeError far from the call.
            return _type_adapter(response_type).validate_python(None)

        raw = response.json()
        if response_type is None:
            return raw
        return _type_adapter(response_type).validate_python(raw)

    async def request_raw(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._execute(path, method=method, body=body, headers=headers, query=query)

        return {
            "content": response.content,
            "mime_type": response.headers.get("content-type", "text/plain"),
        }

    async def stream_sse(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Open a Server-Sent Events stream, yielding parsed ``{"event", "data"}``.

        Backs the generated async ``stream()`` methods for ``x-sdk-streaming``
        endpoints. Raises :class:`ApiError` on a non-2xx response before the
        stream opens. (Token refresh is not retried mid-stream.)
        """
        auth_prefix = f"{DEFAULT_API_PREFIX}/auth/"
        if self._refresh_only and not path.startswith(auth_prefix):
            raise RuntimeError(
                f"Refresh-only HTTP client cannot make requests outside {auth_prefix}"
            )

        timeout = _resolve_timeout(method, path)
        url = f"{self._base_url}{self._transform_path(path)}"
        sends_body = body is not None and method not in ("GET", "HEAD")
        req_headers = {**self._default_headers, "Accept": "text/event-stream"}
        if sends_body:
            req_headers["Content-Type"] = "application/json"
        token = self._get_token()
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        if headers:
            req_headers.update(headers)
        params = _encode_query(query)

        async with self._client.stream(
            method,
            url,
            json=body if sends_body else None,
            headers=req_headers,
            params=params,
            timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raw: dict[str, Any] = {}
                try:
                    raw = response.json()
                except Exception:
                    pass
                code, message = _parse_error(raw, response.status_code)
                raise ApiError(response.status_code, code, message, raw)

            event: str | None = None
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    parsed = _build_sse_event(event, data_lines)
                    if parsed is not None:
                        yield parsed
                    event, data_lines = None, []
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            parsed = _build_sse_event(event, data_lines)
            if parsed is not None:
                yield parsed

    async def close(self) -> None:
        await self._client.aclose()


class SyncHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str | None = None,
        get_access_token: Callable[[], str | None] | None = None,
        on_refresh_token: Callable[[], str] | None = None,
        path_prefix: str | None = None,
        default_headers: dict[str, str] | None = None,
        refresh_only: bool = False,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._get_access_token = get_access_token
        self._on_refresh_token = on_refresh_token
        self._path_prefix = path_prefix
        self._default_headers = default_headers or {}
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)
        self._refresh_only = refresh_only
        self._refresh_lock = threading.Lock()

    def _get_token(self) -> str | None:
        if self._get_access_token:
            return self._get_access_token()
        return self._access_token

    def _transform_path(self, path: str) -> str:
        if self._path_prefix is None:
            return path
        if path.startswith(DEFAULT_API_PREFIX):
            return self._path_prefix + path[len(DEFAULT_API_PREFIX) :]
        return path

    def set_access_token(self, token: str) -> None:
        self._access_token = token

    def set_refresh_handler(self, handler: Callable[[], str]) -> None:
        self._on_refresh_token = handler

    def _do_fetch(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> httpx.Response:
        timeout = _resolve_timeout(method, path)
        token = self._get_token()
        url = f"{self._base_url}{self._transform_path(path)}"

        req_headers = {
            **self._default_headers,
            "Content-Type": "application/json",
        }
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        if headers:
            req_headers.update(headers)

        params = _encode_query(query)

        return self._client.request(
            method,
            url,
            json=body if body is not None and method not in ("GET", "HEAD") else None,
            headers=req_headers,
            params=params,
            timeout=timeout,
        )

    def _execute(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> httpx.Response:
        auth_prefix = f"{DEFAULT_API_PREFIX}/auth/"
        if self._refresh_only and not path.startswith(auth_prefix):
            raise RuntimeError(
                f"Refresh-only HTTP client cannot make requests outside {auth_prefix}"
            )

        original_token = self._get_token()
        response = self._do_fetch(path, method=method, body=body, headers=headers, query=query)

        if (
            response.status_code == 401
            and self._on_refresh_token
            and not path.startswith(auth_prefix)
        ):
            try:
                with self._refresh_lock:
                    if self._get_token() == original_token:
                        self._access_token = self._on_refresh_token()
            except Exception:
                pass
            else:
                response = self._do_fetch(
                    path, method=method, body=body, headers=headers, query=query
                )

        if response.status_code >= 400:
            raw_data: dict[str, Any] = {}
            try:
                raw_data = response.json()
            except Exception:
                pass
            error_code, message = _parse_error(raw_data, response.status_code)
            raise ApiError(response.status_code, error_code, message, raw_data)

        return response

    @overload
    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        response_type: type[T],
    ) -> T: ...

    @overload
    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        response_type: None = None,
    ) -> Any: ...

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        response_type: type[T] | None = None,
    ) -> T | Any:
        """Issue a request and return the JSON body.

        With `response_type`, the body is validated into that type (a Pydantic
        model or a generic alias like list[Model]); a bodyless 204 then raises
        ValidationError, since the operation promised a typed body. Without
        `response_type`, the raw parsed JSON is returned, or None on 204.
        """
        response = self._execute(path, method=method, body=body, headers=headers, query=query)

        if response.status_code == 204:
            if response_type is None:
                return None
            # A 204 on an operation that promises a typed body is a server
            # contract violation; validating None fails loudly here instead
            # of surfacing later as an AttributeError far from the call.
            return _type_adapter(response_type).validate_python(None)

        raw = response.json()
        if response_type is None:
            return raw
        return _type_adapter(response_type).validate_python(raw)

    def request_raw(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._execute(path, method=method, body=body, headers=headers, query=query)

        return {
            "content": response.content,
            "mime_type": response.headers.get("content-type", "text/plain"),
        }

    def stream_sse_sync(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Synchronous counterpart to :meth:`HttpClient.stream_sse`.

        Backs the generated sync ``stream()`` methods. Raises :class:`ApiError`
        on a non-2xx response before the stream opens.
        """
        auth_prefix = f"{DEFAULT_API_PREFIX}/auth/"
        if self._refresh_only and not path.startswith(auth_prefix):
            raise RuntimeError(
                f"Refresh-only HTTP client cannot make requests outside {auth_prefix}"
            )

        timeout = _resolve_timeout(method, path)
        url = f"{self._base_url}{self._transform_path(path)}"
        sends_body = body is not None and method not in ("GET", "HEAD")
        req_headers = {**self._default_headers, "Accept": "text/event-stream"}
        if sends_body:
            req_headers["Content-Type"] = "application/json"
        token = self._get_token()
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        if headers:
            req_headers.update(headers)
        params = _encode_query(query)

        with self._client.stream(
            method,
            url,
            json=body if sends_body else None,
            headers=req_headers,
            params=params,
            timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                response.read()
                raw: dict[str, Any] = {}
                try:
                    raw = response.json()
                except Exception:
                    pass
                code, message = _parse_error(raw, response.status_code)
                raise ApiError(response.status_code, code, message, raw)

            event: str | None = None
            data_lines: list[str] = []
            for line in response.iter_lines():
                if line == "":
                    parsed = _build_sse_event(event, data_lines)
                    if parsed is not None:
                        yield parsed
                    event, data_lines = None, []
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            parsed = _build_sse_event(event, data_lines)
            if parsed is not None:
                yield parsed

    def close(self) -> None:
        self._client.close()


def _build_sse_event(event: str | None, data_lines: list[str]) -> dict[str, Any] | None:
    """Assemble one SSE frame into ``{"event", "data"}``; ``None`` if empty."""
    if event is None and not data_lines:
        return None
    raw = "\n".join(data_lines)
    try:
        data: Any = json.loads(raw)
    except Exception:
        data = raw
    return {"event": event or "message", "data": data}


def _parse_error(raw_data: dict[str, Any], status: int) -> tuple[str, str]:
    error = raw_data.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or "unknown_error"
        message = error.get("message") or f"HTTP {status}"
        return code, message
    error_str = error if isinstance(error, str) else None
    message = raw_data.get("message") or error_str or f"HTTP {status}"
    return error_str or "unknown_error", message
