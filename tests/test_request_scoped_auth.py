import asyncio
import json
import unittest
from unittest import mock

import weknora_mcp_server as server
import weknora_mcp_auth as auth


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
    async def test_request_client_uses_only_current_request_key(self):
        reset = auth.bind_request_api_key("tenant-a")
        try:
            client = server._CLIENT_PROVIDER.get_client()
            self.assertEqual("tenant-a", client.session.headers["X-API-Key"])
        finally:
            client.close()
            auth.reset_request_api_key(reset)

    async def test_streamable_http_tasks_and_workers_inherit_each_request_key(self):
        test_server = server.MCPServer("request-key-propagation-test")

        @test_server.tool()
        async def echo_request_key() -> str:
            await asyncio.sleep(0)
            return auth.current_request_api_key()

        @test_server.tool()
        def echo_request_key_sync() -> str:
            return auth.current_request_api_key()

        async def validator(_key):
            return True

        inner_app = test_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
        )
        app = auth.MCPAuthMiddleware(
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

    async def test_run_http_wires_configured_path_into_production_server(self):
        settings = auth.AuthSettings.from_env(
            {
                "MCP_AUTH_MODE": "weknora_api_key",
                "MCP_HTTP_PATH": "/custom-mcp",
            }
        )
        inner_app = object()
        serve = mock.AsyncMock()

        with (
            mock.patch.object(server, "AUTH_SETTINGS", settings),
            mock.patch.object(
                server.mcp,
                "streamable_http_app",
                return_value=inner_app,
            ) as streamable_http_app,
            mock.patch("uvicorn.Server.serve", serve),
        ):
            await server.run_http("127.0.0.1", 18086)

        streamable_http_app.assert_called_once_with(
            host="127.0.0.1",
            streamable_http_path="/custom-mcp",
            stateless_http=True,
        )
        serve.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
