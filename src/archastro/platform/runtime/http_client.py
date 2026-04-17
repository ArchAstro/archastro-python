# Runtime: async HTTP client for the generated Platform SDK.
# This file is hand-maintained, not generated.

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

DEFAULT_API_PREFIX = "/api/v1"


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
    ):
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._get_access_token = get_access_token
        self._on_refresh_token = on_refresh_token
        self._path_prefix = path_prefix
        self._default_headers = default_headers or {}
        self._client = httpx.AsyncClient(timeout=30.0)
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

        params = None
        if query:
            params = {k: v for k, v in query.items() if v is not None}

        return await self._client.request(
            method,
            url,
            json=body if body is not None and method not in ("GET", "HEAD") else None,
            headers=req_headers,
            params=params,
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

    async def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        response = await self._execute(path, method=method, body=body, headers=headers, query=query)

        if response.status_code == 204:
            return None

        return response.json()

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

    async def close(self) -> None:
        await self._client.aclose()


def _parse_error(raw_data: dict[str, Any], status: int) -> tuple[str, str]:
    error = raw_data.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or "unknown_error"
        message = error.get("message") or f"HTTP {status}"
        return code, message
    error_str = error if isinstance(error, str) else None
    message = raw_data.get("message") or error_str or f"HTTP {status}"
    return error_str or "unknown_error", message
