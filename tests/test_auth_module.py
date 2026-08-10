import asyncio
import unittest

from mcp.server import MCPServer
from weknora_mcp_auth import (
    AuthConfigurationError,
    AuthSettings,
    MCPAuthMiddleware,
    RequestScopedClientProxy,
    RequestScopedClientProvider,
    WeKnoraAPIKeyValidator,
    apply_read_only_tool_policy,
    bind_request_api_key,
    current_request_api_key,
    reset_request_api_key,
)


async def invoke_asgi(app, api_key=None, extra_headers=None):
    headers = [(b"host", b"127.0.0.1:8000")]
    if api_key is not None:
        headers.append((b"authorization", f"Bearer {api_key}".encode("latin-1")))
    headers.extend(extra_headers or [])
    scope = {"type": "http", "headers": headers}
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def response_status(messages):
    return next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )


class RequestScopedAuthModuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_bearer_key_is_unauthorized(self):
        async def downstream(scope, receive, send):
            self.fail("unauthenticated request reached downstream app")

        async def validator(_key):
            return True

        middleware = MCPAuthMiddleware(
            downstream,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )

        messages = await invoke_asgi(middleware)

        self.assertEqual(401, response_status(messages))

    async def test_x_api_key_header_is_not_a_second_public_auth_channel(self):
        async def validator(_key):
            return True

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = MCPAuthMiddleware(
            downstream,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )
        messages = await invoke_asgi(
            middleware,
            extra_headers=[(b"x-api-key", b"tenant-key")],
        )

        self.assertEqual(401, response_status(messages))

    async def test_invalid_key_is_forbidden_without_echoing_secret(self):
        async def downstream(scope, receive, send):
            self.fail("invalid key reached downstream app")

        async def validator(_key):
            return False

        secret = "tenant-key-must-not-leak"
        middleware = MCPAuthMiddleware(
            downstream,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )

        messages = await invoke_asgi(middleware, api_key=secret)
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )

        self.assertEqual(403, response_status(messages))
        self.assertNotIn(secret.encode(), body)

    async def test_overlapping_requests_keep_api_keys_isolated(self):
        entered = 0
        both_entered = asyncio.Event()
        observations = []

        async def validator(_key):
            return True

        async def downstream(scope, receive, send):
            nonlocal entered
            before = current_request_api_key()
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            await asyncio.sleep(0)
            observations.append((before, current_request_api_key()))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = MCPAuthMiddleware(
            downstream,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )

        await asyncio.gather(
            invoke_asgi(middleware, api_key="tenant-a"),
            invoke_asgi(middleware, api_key="tenant-b"),
        )

        self.assertCountEqual(
            [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")], observations
        )

    async def test_request_client_uses_only_current_request_key(self):
        class Client:
            def __init__(self, key):
                self.key = key

        provider = RequestScopedClientProvider(
            static_client=Client("static-key"),
            client_factory=Client,
            request_scoped_mode=lambda: True,
        )
        reset = bind_request_api_key("tenant-a")
        try:
            client = provider.get_client()
            self.assertEqual("tenant-a", client.key)
            self.assertIs(client, provider.get_client())
        finally:
            reset_request_api_key(reset)

        with self.assertRaises(PermissionError):
            provider.get_client()

    async def test_validation_cache_is_bounded_and_stores_no_raw_key(self):
        class Response:
            status_code = 200

        def request_get(*args, **kwargs):
            return Response()

        validator = WeKnoraAPIKeyValidator(
            base_url="http://app:8080/api/v1",
            verify_ssl=True,
            ttl_seconds=60,
            cache_max_entries=2,
            max_concurrency=1,
            request_get=request_get,
        )

        for raw_key in ("raw-tenant-a", "raw-tenant-b", "raw-tenant-c"):
            self.assertTrue(await validator(raw_key))

        fingerprints = tuple(validator._cache)
        self.assertEqual(2, len(fingerprints))
        self.assertNotIn("raw-tenant", " ".join(fingerprints))

    async def test_rate_limited_validation_is_not_cached(self):
        class Response:
            status_code = 429

        validator = WeKnoraAPIKeyValidator(
            base_url="http://app:8080/api/v1",
            verify_ssl=True,
            ttl_seconds=60,
            cache_max_entries=8,
            max_concurrency=1,
            request_get=lambda *args, **kwargs: Response(),
        )

        self.assertIsNone(await validator("rate-limited-key"))
        self.assertEqual({}, validator._cache)

    async def test_settings_validate_dynamic_http_and_reject_dynamic_sse(self):
        settings = AuthSettings.from_env(
            {
                "MCP_AUTH_MODE": "weknora_api_key",
                "MCP_HTTP_PATH": "tenant-mcp/",
                "MCP_READ_ONLY": "true",
                "MCP_API_KEY_VALIDATION_CACHE_MAX_ENTRIES": "2",
            }
        )

        self.assertEqual("/tenant-mcp", settings.http_path)
        self.assertTrue(settings.read_only)
        self.assertEqual(16, settings.validation_cache_max_entries)
        self.assertEqual("", settings.network_token_for("http"))
        with self.assertRaises(AuthConfigurationError):
            settings.network_token_for("sse")

    async def test_shared_secret_mode_preserves_official_auth_headers(self):
        reached = 0

        async def downstream(scope, receive, send):
            nonlocal reached
            reached += 1
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = MCPAuthMiddleware(
            downstream,
            token="official-shared-secret",
            auth_mode="shared_secret",
        )

        rejected = await invoke_asgi(middleware, api_key="wrong-secret")
        bearer = await invoke_asgi(middleware, api_key="official-shared-secret")
        legacy = await invoke_asgi(
            middleware,
            extra_headers=[(b"x-mcp-auth-token", b"official-shared-secret")],
        )

        self.assertEqual(401, response_status(rejected))
        self.assertEqual(204, response_status(bearer))
        self.assertEqual(204, response_status(legacy))
        self.assertEqual(2, reached)

    async def test_read_only_policy_removes_tools_outside_allowlist(self):
        test_server = MCPServer("modular-read-only-policy-test")

        @test_server.tool(name="list_knowledge_bases")
        def allowed_tool() -> dict:
            return {}

        @test_server.tool(name="create_tenant")
        def blocked_tool() -> dict:
            return {}

        await apply_read_only_tool_policy(test_server, enabled=True)

        names = [tool.name for tool in await test_server.list_tools()]
        self.assertEqual(["list_knowledge_bases"], names)

    async def test_client_proxy_delegates_to_request_scoped_provider(self):
        class Client:
            def __init__(self, key):
                self.key = key

            def identity(self):
                return self.key

        provider = RequestScopedClientProvider(
            static_client=Client("static-key"),
            client_factory=Client,
            request_scoped_mode=lambda: True,
        )
        proxy = RequestScopedClientProxy(provider)
        reset = bind_request_api_key("tenant-a")
        try:
            self.assertEqual("tenant-a", proxy.identity())
        finally:
            reset_request_api_key(reset)

    async def test_middleware_closes_request_scoped_client(self):
        created = []

        class Client:
            def __init__(self, key):
                self.key = key
                self.closed = False
                created.append(self)

            def close(self):
                self.closed = True

        provider = RequestScopedClientProvider(
            static_client=Client("static-key"),
            client_factory=Client,
            request_scoped_mode=lambda: True,
        )

        async def validator(_key):
            return True

        async def downstream(scope, receive, send):
            self.assertEqual("tenant-a", provider.get_client().key)
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = MCPAuthMiddleware(
            downstream,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )

        messages = await invoke_asgi(middleware, api_key="tenant-a")

        self.assertEqual(204, response_status(messages))
        self.assertTrue(created[-1].closed)


if __name__ == "__main__":
    unittest.main()
