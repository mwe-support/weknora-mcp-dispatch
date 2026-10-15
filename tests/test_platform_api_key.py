import asyncio
import json
import unittest
from unittest import mock

from mcp.server import MCPServer
import weknora_mcp_auth as auth
import weknora_mcp_server as server
from test_request_scoped_auth import invoke_asgi, response_body, response_status


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


def validator(request_get):
    return auth.WeKnoraAPIKeyValidator(
        base_url="http://app:8080/api/v1", verify_ssl=True, ttl_seconds=60,
        cache_max_entries=8, max_concurrency=2, request_get=request_get,
    )


class PlatformValidationTests(unittest.TestCase):
    def test_authenticated_platform_key_can_initialize_without_workspace(self):
        probe = validator(lambda *a, **k: Response(409, {"code": "TENANT_REQUIRED"}))
        self.assertTrue(probe.validate_sync("platform-key"))

    def test_other_conflicts_do_not_authenticate(self):
        for body in ({"code": "OTHER"}, None, [], {"error": "TENANT_REQUIRED"}):
            with self.subTest(body=body):
                probe = validator(lambda *a, **k: Response(409, body))
                self.assertFalse(probe.validate_sync("bad-key"))

    def test_workspace_is_forwarded_and_validation_cache_cannot_cross_workspaces(self):
        seen = []

        def backend(url, **kwargs):
            self.assertEqual("http://app:8080/api/v1/knowledge-bases", url)
            headers = kwargs["headers"]
            seen.append(dict(headers))
            return Response(200 if headers.get("X-Tenant-ID") == "42" else 403)

        probe = validator(backend)
        self.assertTrue(probe.validate_sync("tenant-key", "42"))
        self.assertFalse(probe.validate_sync("tenant-key", "43"))
        self.assertTrue(probe.validate_sync("tenant-key", "42"))
        self.assertEqual(["42", "43"], [h["X-Tenant-ID"] for h in seen])
        self.assertNotIn("tenant-key", repr(probe._cache))

    def test_targeted_conflict_is_not_accepted_as_tenantless_authentication(self):
        probe = validator(lambda *a, **k: Response(409, {"code": "TENANT_REQUIRED"}))
        self.assertFalse(probe.validate_sync("platform-key", "42"))

    def test_client_preserves_workspace_on_worker_thread_sessions(self):
        client = server.WeKnoraClient("http://app/api/v1", "platform-key", tenant_id="42")
        try:
            self.assertEqual("42", client.session.headers["X-Tenant-ID"])
            self.assertEqual("platform-key", client.session.headers["X-API-Key"])
        finally:
            client.close()

    def test_all_space_discovery_is_explicit_and_legacy_listing_stays_local(self):
        client = server.WeKnoraClient("http://app/api/v1", "platform-key")
        with mock.patch.object(client, "_request", return_value={}) as request:
            client.list_tenants()
            request.assert_called_with("GET", "/tenants")
            client.list_tenants(all_spaces=True)
            request.assert_called_with("GET", "/tenants/all")
        with mock.patch.object(server, "client") as proxy:
            server.list_tenants(all_spaces=True)
            proxy.list_tenants.assert_called_once_with(all_spaces=True)


class PlatformHTTPIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tenantless_platform_request_can_discover_all_spaces_through_mcp(self):
        mcp = MCPServer("platform-discovery-test")

        @mcp.tool()
        def discover() -> dict:
            return server.list_tenants(all_spaces=True)

        inner = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
        app = auth.MCPAuthMiddleware(inner, token="", auth_mode="weknora_api_key",
                                     api_key_validator=validator(lambda *a, **k: Response(409, {"code": "TENANT_REQUIRED"})))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "discover", "arguments": {}}}).encode()
        async with inner.router.lifespan_context(inner):
            with mock.patch.object(server.WeKnoraClient, "_request", return_value={"success": True, "data": [{"id": 42}]}) as request:
                reply = await invoke_asgi(app, api_key="platform-key", body=body)
                self.assertEqual(200, response_status(reply))
                result = json.loads(response_body(reply))["result"]
                self.assertFalse(result.get("isError", False), result)
                self.assertEqual({"success": True, "data": [{"id": 42}]}, json.loads(result["content"][0]["text"]))
                request.assert_called_once_with("GET", "/tenants/all")

    async def test_invalid_duplicate_or_out_of_range_workspace_header_fails_closed(self):
        async def backend(*args):
            self.fail("invalid workspace header reached validation")

        async def downstream(*args):
            self.fail("invalid workspace header reached MCP")

        app = auth.MCPAuthMiddleware(downstream, token="", auth_mode="weknora_api_key", api_key_validator=backend)
        for value in (b"", b"0", b"-1", b"1.0", b"abc", b"18446744073709551616", b"\xb2"):
            with self.subTest(value=value):
                response = await invoke_asgi(app, api_key="platform-key", extra_headers=[(b"x-tenant-id", value)])
                self.assertEqual(400, response_status(response))
        response = await invoke_asgi(app, api_key="platform-key", extra_headers=[(b"x-tenant-id", b"42"), (b"x-tenant-id", b"43")])
        self.assertEqual(400, response_status(response))

    async def test_workspace_key_cannot_switch_and_rejection_does_not_poison_own_workspace(self):
        def backend(url, **kwargs):
            return Response(200 if kwargs["headers"].get("X-Tenant-ID") == "42" else 403)

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        app = auth.MCPAuthMiddleware(downstream, token="", auth_mode="weknora_api_key", api_key_validator=validator(backend))
        for tenant, expected in ((b"43", 403), (b"42", 204)):
            messages = await invoke_asgi(app, api_key="tenant-key", extra_headers=[(b"x-tenant-id", tenant)])
            self.assertEqual(expected, response_status(messages))

    async def test_same_platform_key_two_workspaces_stays_isolated_in_mcp_tasks_and_workers(self):
        mcp = MCPServer("platform-workspace-isolation-test")

        @mcp.tool()
        async def async_headers() -> str:
            before = dict(server.client.session.headers)
            await asyncio.sleep(0.01)
            after = dict(server.client.session.headers)
            return json.dumps([before.get("X-Tenant-ID"), after.get("X-Tenant-ID")])

        @mcp.tool()
        def worker_headers() -> str:
            return server.client.session.headers.get("X-Tenant-ID", "missing")

        inner = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
        app = auth.MCPAuthMiddleware(inner, token="", auth_mode="weknora_api_key",
                                     api_key_validator=validator(lambda *a, **k: Response(200)))
        async with inner.router.lifespan_context(inner):
            for tool in ("async_headers", "worker_headers"):
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": {}}}).encode()
                replies = await asyncio.gather(*(
                    invoke_asgi(app, api_key="same-platform-key", body=body,
                                extra_headers=[(b"x-tenant-id", tenant.encode())])
                    for tenant in ("42", "43")
                ))
                for tenant, reply in zip(("42", "43"), replies):
                    self.assertEqual(200, response_status(reply))
                    result = json.loads(response_body(reply))["result"]
                    self.assertFalse(result.get("isError", False), result)
                    value = result["content"][0]["text"]
                    self.assertEqual(json.dumps([tenant, tenant]) if tool == "async_headers" else tenant, value)
        self.assertEqual("", auth.current_request_api_key())


if __name__ == "__main__":
    unittest.main()
