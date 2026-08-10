"""Authentication adapter for network WeKnora MCP transports."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.exceptions import RequestException


logger = logging.getLogger(__name__)


AUTH_MODE_SHARED_SECRET = "shared_secret"
AUTH_MODE_WEKNORA_API_KEY = "weknora_api_key"
READ_ONLY_TOOL_NAMES = frozenset(
    {
        "list_knowledge_bases",
        "list_shared_knowledge_bases",
        "get_knowledge_base",
        "hybrid_search",
        "list_knowledge",
        "get_knowledge",
        "list_chunks",
        "list_agents",
        "get_agent",
        "wiki_search",
        "wiki_read_page",
        "wiki_index_view",
    }
)


class AuthConfigurationError(ValueError):
    """Raised when a network authentication mode is unsafe or incomplete."""


def _normalize_path_prefix(path_prefix: str) -> str:
    normalized = (path_prefix or "/").strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/") or "/"


def _positive_int(
    values: Mapping[str, str], name: str, default: int, *, minimum: int = 1
) -> int:
    try:
        return max(minimum, int(values.get(name, str(default))))
    except ValueError:
        logger.warning("%s is invalid; falling back to %d", name, default)
        return default


@dataclass(frozen=True)
class AuthSettings:
    auth_mode: str
    server_auth_token: str
    read_only: bool
    http_path: str
    verify_ssl: bool
    validation_ttl_seconds: int
    validation_cache_max_entries: int
    validation_max_concurrency: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AuthSettings":
        values = os.environ if environ is None else environ
        return cls(
            auth_mode=values.get("MCP_AUTH_MODE", AUTH_MODE_SHARED_SECRET)
            .strip()
            .lower(),
            server_auth_token=values.get("MCP_SERVER_AUTH_TOKEN", "").strip(),
            read_only=values.get("MCP_READ_ONLY", "false").lower() == "true",
            http_path=_normalize_path_prefix(values.get("MCP_HTTP_PATH", "/mcp")),
            verify_ssl=values.get("WEKNORA_VERIFY_SSL", "true").lower() != "false",
            validation_ttl_seconds=_positive_int(
                values, "MCP_API_KEY_VALIDATION_TTL_SECONDS", 60
            ),
            validation_cache_max_entries=_positive_int(
                values,
                "MCP_API_KEY_VALIDATION_CACHE_MAX_ENTRIES",
                2048,
                minimum=16,
            ),
            validation_max_concurrency=_positive_int(
                values, "MCP_API_KEY_VALIDATION_MAX_CONCURRENCY", 16
            ),
        )

    @property
    def request_scoped(self) -> bool:
        return self.auth_mode == AUTH_MODE_WEKNORA_API_KEY

    def network_token_for(self, transport: str) -> str:
        if self.auth_mode not in {
            AUTH_MODE_SHARED_SECRET,
            AUTH_MODE_WEKNORA_API_KEY,
        }:
            raise AuthConfigurationError(
                f"Unsupported MCP_AUTH_MODE={self.auth_mode!r}; use "
                "shared_secret or weknora_api_key"
            )
        if self.request_scoped:
            if transport != "http":
                raise AuthConfigurationError(
                    "MCP_AUTH_MODE=weknora_api_key is only supported by the "
                    "stateless Streamable HTTP transport"
                )
            return ""
        if transport in {"sse", "http"} and not self.server_auth_token:
            raise AuthConfigurationError(
                f"MCP_SERVER_AUTH_TOKEN is required for {transport} transport"
            )
        return self.server_auth_token


@dataclass
class RequestAuthContext:
    api_key: str
    client: Any | None = None
    client_lock: threading.Lock = field(default_factory=threading.Lock)


_REQUEST_AUTH_CONTEXT: contextvars.ContextVar[RequestAuthContext | None] = (
    contextvars.ContextVar("weknora_request_auth_context", default=None)
)


def current_request_api_key() -> str:
    """Return the WeKnora API key bound to the current request task."""
    context = _REQUEST_AUTH_CONTEXT.get()
    return context.api_key if context else ""


def bind_request_api_key(api_key: str):
    """Bind an API key to the current async context and return its reset token."""
    return _REQUEST_AUTH_CONTEXT.set(RequestAuthContext(api_key))


def reset_request_api_key(token) -> None:
    """Restore the previous request API key context."""
    _REQUEST_AUTH_CONTEXT.reset(token)


class RequestScopedClientProvider:
    """Resolve one backend client for the active authenticated request."""

    def __init__(
        self,
        static_client: Any,
        client_factory: Callable[[str], Any],
        request_scoped_mode: Callable[[], bool],
    ):
        self.static_client = static_client
        self.client_factory = client_factory
        self.request_scoped_mode = request_scoped_mode

    def get_client(self):
        context = _REQUEST_AUTH_CONTEXT.get()
        if context is not None:
            with context.client_lock:
                if context.client is None:
                    context.client = self.client_factory(context.api_key)
                return context.client
        if self.request_scoped_mode():
            raise PermissionError("No request-scoped WeKnora API key is available")
        return self.static_client


class RequestScopedClientProxy:
    """Delegate backend methods to the client selected for this request."""

    def __init__(self, provider: RequestScopedClientProvider):
        self.provider = provider

    def __getattr__(self, name: str):
        return getattr(self.provider.get_client(), name)


class WeKnoraAPIKeyValidator:
    """Validate WeKnora API keys with a bounded fingerprint-only cache."""

    def __init__(
        self,
        *,
        base_url: str,
        verify_ssl: bool,
        ttl_seconds: int,
        cache_max_entries: int,
        max_concurrency: int,
        request_get: Callable[..., Any] = requests.get,
    ):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.ttl_seconds = max(1, ttl_seconds)
        self.cache_max_entries = max(1, cache_max_entries)
        self.request_get = request_get
        self._cache: dict[str, tuple[float, bool]] = {}
        self._cache_lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    @staticmethod
    def _fingerprint(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    def _cache_result(self, fingerprint: str, expires_at: float, valid: bool) -> None:
        now = time.monotonic()
        with self._cache_lock:
            if len(self._cache) >= self.cache_max_entries:
                expired = [
                    key for key, (expiry, _) in self._cache.items() if expiry <= now
                ]
                for key in expired:
                    self._cache.pop(key, None)
            while len(self._cache) >= self.cache_max_entries:
                self._cache.pop(next(iter(self._cache)))
            self._cache[fingerprint] = (expires_at, valid)

    def validate_sync(self, api_key: str) -> bool | None:
        fingerprint = self._fingerprint(api_key)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(fingerprint)
        if cached and cached[0] > now:
            return cached[1]

        try:
            response = self.request_get(
                f"{self.base_url}/knowledge-bases",
                headers={"X-API-Key": api_key, "Accept": "application/json"},
                timeout=(3, 10),
                verify=self.verify_ssl,
            )
        except RequestException as exc:
            logger.error("WeKnora API key validation is unavailable: %s", exc)
            return None

        if 200 <= response.status_code < 300:
            valid = True
        elif response.status_code in {408, 425, 429}:
            logger.warning(
                "WeKnora API key validation was limited with status %d",
                response.status_code,
            )
            return None
        elif 400 <= response.status_code < 500:
            valid = False
        else:
            logger.error(
                "WeKnora API key validation returned upstream status %d",
                response.status_code,
            )
            return None

        ttl = self.ttl_seconds if valid else min(10, self.ttl_seconds)
        self._cache_result(fingerprint, now + ttl, valid)
        return valid

    async def __call__(self, api_key: str) -> bool | None:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=1.0)
        except TimeoutError:
            logger.warning("WeKnora API key validation concurrency limit reached")
            return None
        try:
            return await asyncio.to_thread(self.validate_sync, api_key)
        finally:
            self._semaphore.release()


async def apply_read_only_tool_policy(server, *, enabled: bool) -> None:
    """Remove tools outside the production query allowlist."""
    if not enabled:
        return
    for tool in await server.list_tools():
        if tool.name not in READ_ONLY_TOOL_NAMES:
            server.remove_tool(tool.name)


class MCPAuthMiddleware:
    """Authenticate MCP HTTP requests before dispatching them to MCPServer."""

    def __init__(
        self,
        app,
        token: str,
        auth_mode: str = AUTH_MODE_SHARED_SECRET,
        api_key_validator: Callable[[str], Awaitable[bool | None]] | None = None,
    ):
        self.app = app
        self.token = token
        self.auth_mode = auth_mode
        self.api_key_validator = api_key_validator

    @staticmethod
    async def _send_error(send, status: int, error: str) -> None:
        body = json.dumps({"error": error}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"www-authenticate", b"Bearer"],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        provided = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else ""
        )
        if (
            not provided
            and self.auth_mode == AUTH_MODE_SHARED_SECRET
            and "x-mcp-auth-token" in headers
        ):
            provided = headers["x-mcp-auth-token"]
        if not provided:
            await self._send_error(send, 401, "missing_api_key")
            return

        if self.auth_mode == AUTH_MODE_SHARED_SECRET:
            if not self.token or not secrets.compare_digest(provided, self.token):
                await self._send_error(send, 401, "unauthorized")
                return
            await self.app(scope, receive, send)
            return

        if self.auth_mode == AUTH_MODE_WEKNORA_API_KEY:
            if self.api_key_validator is None:
                await self._send_error(send, 500, "invalid_server_auth_mode")
                return
            validation = await self.api_key_validator(provided)
            if validation is None:
                await self._send_error(send, 503, "authentication_service_unavailable")
                return
            if not validation:
                await self._send_error(send, 403, "invalid_or_unscoped_api_key")
                return

            context_token = bind_request_api_key(provided)
            request_context = _REQUEST_AUTH_CONTEXT.get()
            try:
                await self.app(scope, receive, send)
            finally:
                try:
                    if request_context and request_context.client is not None:
                        request_context.client.close()
                finally:
                    reset_request_api_key(context_token)
            return

        await self._send_error(send, 500, "invalid_server_auth_mode")
