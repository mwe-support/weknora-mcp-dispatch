import asyncio
import json
import unittest
from unittest import mock

import weknora_mcp_server as server


async def invoke_asgi(app, path="/mcp", api_key=None, body=b"", extra_headers=None):
    headers = [
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

    async def test_streamable_http_task_inherits_each_request_key(self):
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Mount

        test_server = server.Server("request-key-propagation-test")

        @test_server.call_tool()
        async def echo_request_key(name, arguments):
            await asyncio.sleep(0)
            return [
                server.types.TextContent(
                    type="text", text=server.current_request_api_key()
                )
            ]

        async def validator(_key):
            return True

        manager = StreamableHTTPSessionManager(
            app=test_server,
            event_store=None,
            json_response=True,
            stateless=True,
        )
        app = Starlette(routes=[Mount("/", app=manager.handle_request)])
        app = server.MCPAuthMiddleware(
            app,
            token="",
            auth_mode="weknora_api_key",
            api_key_validator=validator,
        )
        app = server.MCPPathPrefixMiddleware(app, "/mcp")
        rpc_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo_request_key", "arguments": {}},
            }
        ).encode()

        async with manager.run():
            responses = await asyncio.gather(
                invoke_asgi(app, api_key="tenant-a", body=rpc_body),
                invoke_asgi(app, api_key="tenant-b", body=rpc_body),
            )

        decoded = [json.loads(response_body(messages)) for messages in responses]
        returned = [item["result"]["content"][0]["text"] for item in decoded]
        self.assertCountEqual(["tenant-a", "tenant-b"], returned)

    async def test_mcp_path_prefix_is_stripped_before_origin(self):
        seen_paths = []

        async def downstream(scope, receive, send):
            seen_paths.append((scope["path"], scope["root_path"]))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = server.MCPPathPrefixMiddleware(downstream, "/mcp")
        messages = await invoke_asgi(middleware, path="/mcp")
        self.assertEqual(204, response_status(messages))
        self.assertEqual([("/", "/mcp")], seen_paths)

        messages = await invoke_asgi(middleware, path="/mcp/")
        self.assertEqual(204, response_status(messages))

        messages = await invoke_asgi(middleware, path="/mcp/extra")
        self.assertEqual(404, response_status(messages))

        messages = await invoke_asgi(middleware, path="/not-mcp")
        self.assertEqual(404, response_status(messages))

    @staticmethod
    async def _ok_app(scope, receive, send):
        body = json.dumps({"ok": True}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    unittest.main()
