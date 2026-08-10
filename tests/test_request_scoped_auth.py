import asyncio
import json
import unittest
from unittest import mock

import weknora_mcp_server as server


async def invoke_asgi(app, path="/mcp", api_key=None, body=b"", extra_headers=None):
    headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    if api_key is not None:
        headers.append((b"authorization", f"Bearer {api_key}".encode("latin-1")))
    headers.extend(extra_headers or [])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def response_status(messages):
    return next(message["status"] for message in messages if message["type"] == "http.response.start")


def response_body(messages):
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )


class RequestScopedApiKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_is_unauthorized(self):
        async def validator(_key):
            return True

        middleware = server.MCPAuthMiddleware(
            self._ok_app,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )
        messages = await invoke_asgi(middleware, api_key=None)
        self.assertEqual(401, response_status(messages))

    async def test_invalid_key_is_forbidden_without_echoing_secret(self):
        async def validator(_key):
            return False

        secret = "tenant-key-must-not-leak"
        middleware = server.MCPAuthMiddleware(
            self._ok_app,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )
        messages = await invoke_asgi(middleware, api_key=secret)
        self.assertEqual(403, response_status(messages))
        self.assertNotIn(secret.encode(), response_body(messages))

    async def test_x_api_key_header_is_not_a_second_public_auth_channel(self):
        async def validator(_key):
            return True

        middleware = server.MCPAuthMiddleware(
            self._ok_app,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )
        messages = await invoke_asgi(
            middleware,
            extra_headers=[(b"x-api-key", b"tenant-key")],
        )
        self.assertEqual(401, response_status(messages))

    async def test_overlapping_requests_keep_api_keys_isolated(self):
        entered = 0
        both_entered = asyncio.Event()
        observations = []

        async def validator(_key):
            return True

        async def downstream(scope, receive, send):
            nonlocal entered
            before = server.current_request_api_key()
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            await asyncio.sleep(0)
            after = server.current_request_api_key()
            observations.append((before, after, scope["path"]))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = server.MCPAuthMiddleware(
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
            [("tenant-a", "tenant-a", "/mcp"), ("tenant-b", "tenant-b", "/mcp")],
            observations,
        )

    async def test_request_client_uses_only_current_request_key(self):
        reset = server.bind_request_api_key("tenant-a")
        try:
            client = server.weknora_client_for_request()
            self.assertEqual("tenant-a", client.session.headers["X-API-Key"])
        finally:
            client.close()
            server.reset_request_api_key(reset)

    async def test_validation_cache_is_bounded_and_stores_no_raw_key(self):
        server._API_KEY_VALIDATION_CACHE.clear()
        with mock.patch.object(server, "MCP_API_KEY_VALIDATION_CACHE_MAX_ENTRIES", 2):
            for raw_key in ("raw-tenant-a", "raw-tenant-b", "raw-tenant-c"):
                server._cache_api_key_validation_result(
                    server._api_key_fingerprint(raw_key),
                    expires_at=float("inf"),
                    valid=True,
                )

        self.assertEqual(2, len(server._API_KEY_VALIDATION_CACHE))
        serialized_keys = " ".join(server._API_KEY_VALIDATION_CACHE)
        self.assertNotIn("raw-tenant", serialized_keys)
        server._API_KEY_VALIDATION_CACHE.clear()

    async def test_rate_limited_validation_is_temporary_unavailable(self):
        response = mock.Mock(status_code=429)
        server._API_KEY_VALIDATION_CACHE.clear()
        with mock.patch.object(server.requests, "get", return_value=response):
            self.assertIsNone(server._validate_weknora_api_key_sync("rate-limited-key"))
        self.assertEqual({}, server._API_KEY_VALIDATION_CACHE)

    async def test_streamable_http_tasks_and_workers_inherit_each_request_key(self):
        test_server = server.MCPServer("request-key-propagation-test")

        @test_server.tool()
        async def echo_request_key() -> str:
            await asyncio.sleep(0)
            return server.current_request_api_key()

        @test_server.tool()
        def echo_request_key_sync() -> str:
            return server.current_request_api_key()

        async def validator(_key):
            return True

        inner_app = test_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
        )
        app = server.MCPAuthMiddleware(
            inner_app,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )
        async with inner_app.router.lifespan_context(inner_app):
            for tool_name in ("echo_request_key", "echo_request_key_sync"):
                rpc_body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": {}},
                    }
                ).encode()
                responses = await asyncio.gather(
                    invoke_asgi(app, api_key="tenant-a", body=rpc_body),
                    invoke_asgi(app, api_key="tenant-b", body=rpc_body),
                )
                decoded = [
                    json.loads(response_body(messages)) for messages in responses
                ]
                returned = [
                    item["result"]["content"][0]["text"] for item in decoded
                ]
                self.assertCountEqual(["tenant-a", "tenant-b"], returned)

    async def test_read_only_policy_prunes_high_level_tools(self):
        test_server = server.MCPServer("read-only-policy-test")

        @test_server.tool(name="list_knowledge_bases")
        def allowed_tool() -> dict:
            return {}

        @test_server.tool(name="create_tenant")
        def blocked_tool() -> dict:
            return {}

        with mock.patch.object(server, "MCP_READ_ONLY", True):
            await server._apply_read_only_policy(test_server)

        names = [tool.name for tool in await test_server.list_tools()]
        self.assertEqual(["list_knowledge_bases"], names)

    async def test_mcp_path_is_configured_on_high_level_server(self):
        test_server = server.MCPServer("custom-path-test")
        app = test_server.streamable_http_app(
            streamable_http_path="/custom-mcp",
            stateless_http=True,
        )
        self.assertEqual(["/custom-mcp"], [route.path for route in app.routes])

    @staticmethod
    async def _ok_app(scope, receive, send):
        body = json.dumps({"ok": True}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    unittest.main()
