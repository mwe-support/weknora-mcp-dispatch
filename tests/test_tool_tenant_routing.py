import asyncio
import inspect
import json
import time
import unittest
from unittest import mock

from mcp.server import MCPServer
from requests.exceptions import HTTPError
import weknora_mcp_auth as auth
import weknora_mcp_server as server
from test_platform_api_key import Response, validator
from test_request_scoped_auth import invoke_asgi, response_body


ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


class ToolTenantRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_static_mode_routes_but_dynamic_mode_never_falls_back_without_auth(self):
        static = server.WeKnoraClient('http://example.test/api/v1', 'configured-key')
        provider = auth.RequestScopedClientProvider(
            static, lambda key: server.WeKnoraClient('http://example.test/api/v1', key, auth.current_request_tenant_id()),
            lambda: True,
        )
        with self.assertRaises(PermissionError):
            with provider.use_tenant(42):
                self.fail('lost authentication context must fail closed')
        for value in (0, -1, True, 1.5, '42', 2**64):
            with self.assertRaises(ValueError):
                with provider.use_tenant(value):
                    self.fail('invalid tenant_id must fail before client creation')
        provider.request_scoped_mode = lambda: False
        with provider.use_tenant(42):
            client = provider.get_client()
            self.assertEqual('configured-key', client.api_key)
            self.assertEqual('42', client.session.headers['X-Tenant-ID'])
        self.assertIs(static, provider.get_client())
        self.assertEqual('', auth.current_request_tenant_id())
        static.close()

    async def test_every_workspace_tool_exposes_optional_constrained_tenant_id(self):
        tools = await server.mcp.list_tools()
        workspace_tools = [tool for tool in tools if tool.name != 'create_tenant']
        self.assertEqual(29, len(workspace_tools))
        for tool in workspace_tools:
            with self.subTest(tool=tool.name):
                field = tool.input_schema['properties']['tenant_id']
                self.assertNotIn('tenant_id', tool.input_schema.get('required', []))
                self.assertEqual(None, field['default'])
                integer = next(item for item in field['anyOf'] if item.get('type') == 'integer')
                self.assertEqual(0, integer['exclusiveMinimum'])
                self.assertEqual(2**64 - 1, integer['maximum'])

    async def test_every_workspace_tool_routes_explicit_parameter_and_restores_header(self):
        tools = await server.mcp.list_tools()
        token = auth.bind_request_api_key('same-platform-key', '41')
        required = dict(name='sample', description='sample', business='sample', kb_id=ID,
                        model_id=ID, session_id=ID, agent_id=ID, knowledge_id=ID, chunk_id=ID,
                        query='sample', file_path='/uploads/sample.txt', url='https://example.test/',
                        title='sample', content='sample', type='embedding', slug='sample')
        try:
            original = server._CLIENT_PROVIDER.get_client()
            for tool in tools:
                if tool.name == 'create_tenant':
                    continue
                with self.subTest(tool=tool.name):
                    function = getattr(server, tool.name)
                    args = {name: required[name] for name, p in inspect.signature(function).parameters.items()
                            if p.default is inspect.Parameter.empty}
                    args['tenant_id'] = 42
                    if 'knowledge_base_ids' in inspect.signature(function).parameters:
                        args['knowledge_base_ids'] = [ID]

                    def observed(client, *a, **k):
                        self.assertEqual('same-platform-key', client.session.headers['X-API-Key'])
                        self.assertEqual('42', client.session.headers['X-Tenant-ID'])
                        return {'success': True, 'data': []}

                    with mock.patch.object(server.WeKnoraClient, tool.name, autospec=True, side_effect=observed):
                        result = function(**args)
                        if inspect.isawaitable(result):
                            result = await result
                        self.assertTrue(result['success'])
                    self.assertEqual('41', auth.current_request_tenant_id())
                    self.assertIs(original, server._CLIENT_PROVIDER.get_client())
                    self.assertEqual('41', original.session.headers['X-Tenant-ID'])
        finally:
            original.close()
            auth.reset_request_api_key(token)

    async def test_default_none_keeps_header_and_tool_failure_closes_only_its_client(self):
        token = auth.bind_request_api_key('platform-key', '41')
        original_close = server.WeKnoraClient.close
        closed = []

        def close(client):
            closed.append(client.tenant_id)
            original_close(client)

        try:
            with mock.patch.object(server.WeKnoraClient, 'list_knowledge_bases', autospec=True,
                                   side_effect=lambda c: {'tenant_id': c.session.headers.get('X-Tenant-ID')}):
                self.assertEqual({'tenant_id': '41'}, server.list_knowledge_bases())
            with mock.patch.object(server.WeKnoraClient, 'close', autospec=True, side_effect=close):
                with mock.patch.object(server.WeKnoraClient, 'list_knowledge_bases', side_effect=RuntimeError('synthetic failure')):
                    with self.assertRaises(RuntimeError):
                        server.list_knowledge_bases(tenant_id=42)
            self.assertEqual(['42'], closed)
            self.assertEqual('41', auth.current_request_tenant_id())
            self.assertEqual('platform-key', auth.current_request_api_key())
        finally:
            server._CLIENT_PROVIDER.get_client().close()
            auth.reset_request_api_key(token)

    async def test_real_mcp_same_connection_parameters_isolate_sync_and_async_workers(self):
        mcp = MCPServer('tool-tenant-isolation')
        for function in (server.list_knowledge_bases, server.chat):
            mcp.tool()(function)
        inner = mcp.streamable_http_app(streamable_http_path='/mcp', json_response=True, stateless_http=True)
        app = auth.MCPAuthMiddleware(inner, token='', auth_mode='weknora_api_key',
                                     api_key_validator=validator(lambda *a, **k: Response(409, {'code': 'TENANT_REQUIRED'})))

        def observed(client, *a, **k):
            before = client.session.headers.get('X-Tenant-ID')
            time.sleep(0.01)
            return {'before': before, 'after': client.session.headers.get('X-Tenant-ID')}

        async with inner.router.lifespan_context(inner):
            for tool in ('list_knowledge_bases', 'chat'):
                with mock.patch.object(server.WeKnoraClient, tool, autospec=True, side_effect=observed):
                    calls = []
                    for tenant in (42, 43):
                        arguments = {'tenant_id': tenant}
                        if tool == 'chat':
                            arguments.update(session_id=ID, query='synthetic', knowledge_base_ids=[ID])
                        body = json.dumps({'jsonrpc': '2.0', 'id': tenant, 'method': 'tools/call',
                                           'params': {'name': tool, 'arguments': arguments}}).encode()
                        calls.append(invoke_asgi(app, api_key='same-platform-key', body=body))
                    for tenant, reply in zip((42, 43), await asyncio.gather(*calls)):
                        result = json.loads(response_body(reply))['result']
                        self.assertFalse(result.get('isError', False), result)
                        self.assertEqual({'before': str(tenant), 'after': str(tenant)}, json.loads(result['content'][0]['text']))
        self.assertEqual('', auth.current_request_tenant_id())

    async def test_invalid_parameters_and_workspace_key_switch_fail_without_scope_leak(self):
        mcp = MCPServer('tool-tenant-boundaries')
        mcp.tool()(server.list_knowledge_bases)
        inner = mcp.streamable_http_app(streamable_http_path='/mcp', json_response=True, stateless_http=True)
        app = auth.MCPAuthMiddleware(inner, token='', auth_mode='weknora_api_key',
                                     api_key_validator=validator(lambda *a, **k: Response(200)))
        seen = []

        def backend(client):
            tenant = client.session.headers.get('X-Tenant-ID')
            seen.append(tenant)
            if tenant != '41':
                raise HTTPError('403 synthetic workspace-key denial')
            return {'success': True, 'data': []}

        async def call(tenant):
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': 'list_knowledge_bases', 'arguments': {'tenant_id': tenant}}}).encode()
            reply = await invoke_asgi(app, api_key='workspace-key', body=body)
            return json.loads(response_body(reply))['result']

        async with inner.router.lifespan_context(inner):
            with mock.patch.object(server.WeKnoraClient, 'list_knowledge_bases', autospec=True, side_effect=backend):
                for value in (0, -1, 2**64, True, 1.5, '42'):
                    with self.subTest(value=value):
                        self.assertTrue((await call(value))['isError'])
                self.assertEqual([], seen)
                self.assertTrue((await call(42))['isError'])
                self.assertFalse((await call(41)).get('isError', False))
                self.assertEqual(['42', '41'], seen)

    async def test_initialize_instructions_explain_discovery_without_user_supplied_ids(self):
        inner = server.mcp.streamable_http_app(streamable_http_path='/mcp', json_response=True, stateless_http=True)
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                           'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                                      'clientInfo': {'name': 'test', 'version': '1'}}}).encode()
        async with inner.router.lifespan_context(inner):
            reply = await invoke_asgi(inner, body=body)
            instructions = json.loads(response_body(reply))['result'].get('instructions', '')
            for term in ('list_tenants', 'all_spaces', 'tenant_id', 'user'):
                self.assertIn(term, instructions)


if __name__ == '__main__':
    unittest.main()
