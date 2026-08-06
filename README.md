# WeKnora MCP Dispatch

面向 WeKnora v0.7.1 的多用户 MCP 网关。一个 Streamable HTTP MCP
服务实例可以接收多个客户端请求，并以客户端提交的 WeKnora API Key
决定其可访问的空间和知识库。

本项目基于 WeKnora MCP Server 改造，重点解决单个服务端静态 API Key
无法实现客户端权限隔离的问题。

## 鉴权链路

```text
MCP client
  ├─ 可选：Cloudflare Access Service Token
  └─ Authorization: Bearer <WeKnora API Key>
          ↓
WeKnora MCP Dispatch
  ├─ 使用只读 /knowledge-bases 请求验证 API Key
  ├─ 将 Key 绑定到当前异步请求上下文
  └─ 以 X-API-Key 转发到 WeKnora API
          ↓
WeKnora v0.7.1
```

不同客户端不共享静态 WeKnora Key。请求级 Key 使用 Python `ContextVar`
隔离，即使请求重叠执行，也不会把一个客户端的身份转发给另一个客户端。

## 安全特性

- 公网 MCP 只接受 `Authorization: Bearer <WeKnora API Key>`。
- 不接受外部 `X-API-Key` 作为第二条认证通道。
- API Key 通过 WeKnora 的只读接口验证。
- 验证缓存只保存 SHA-256 指纹，不保存原始 Key。
- 缓存条目数、TTL 和验证并发均有上限。
- 上游临时限流或不可用返回 `503`，不会误判 Key 永久无效。
- `MCP_READ_ONLY=true` 时只注册/允许知识库查询工具。
- `MCP_HTTP_PATH=/mcp` 时仅接受 `/mcp` 与 `/mcp/`。
- 动态 Key 模式不会回退到 `WEKNORA_API_KEY` 静态环境变量。

## 只读工具

生产只读模式允许：

- `list_knowledge_bases`
- `get_knowledge_base`
- `hybrid_search`
- `list_knowledge`
- `get_knowledge`
- `list_chunks`
- `list_agents`
- `get_agent`
- `wiki_search`
- `wiki_read_page`
- `wiki_index_view`

## Docker 运行

```bash
docker build -t weknora-mcp-dispatch:0.1.0 .

docker run --rm \
  --name weknora-mcp-dispatch \
  -p 127.0.0.1:18082:8000 \
  -e WEKNORA_BASE_URL=http://app:8080/api/v1 \
  -e MCP_AUTH_MODE=weknora_api_key \
  -e MCP_READ_ONLY=true \
  -e MCP_HTTP_PATH=/mcp \
  -e MCP_API_KEY_VALIDATION_TTL_SECONDS=60 \
  -e MCP_API_KEY_VALIDATION_CACHE_MAX_ENTRIES=2048 \
  -e MCP_API_KEY_VALIDATION_MAX_CONCURRENCY=16 \
  -e WEKNORA_API_KEY= \
  -e MCP_SERVER_AUTH_TOKEN= \
  weknora-mcp-dispatch:0.1.0
```

如使用 WeKnora 官方 Compose，可参考
[`production.request-scoped.override.yml`](./production.request-scoped.override.yml)
为现有 `mcp` 服务启用请求级鉴权。

## 客户端请求头

直接访问 MCP：

```http
Authorization: Bearer <WEKNORA_SPACE_API_KEY>
```

如果外层使用 Cloudflare Access Service Token：

```http
CF-Access-Client-Id: <CLOUDFLARE_SERVICE_TOKEN_CLIENT_ID>
CF-Access-Client-Secret: <CLOUDFLARE_SERVICE_TOKEN_CLIENT_SECRET>
Authorization: Bearer <WEKNORA_SPACE_API_KEY>
```

Cloudflare 负责公网入口鉴权，WeKnora API Key 负责用户/空间权限控制；两层可以同时启用。

## 环境变量

| 变量 | 推荐值 | 说明 |
| --- | --- | --- |
| `WEKNORA_BASE_URL` | `http://app:8080/api/v1` | WeKnora API 地址 |
| `MCP_AUTH_MODE` | `weknora_api_key` | 启用请求级 WeKnora Key |
| `MCP_READ_ONLY` | `true` | 禁止写入类 MCP 工具 |
| `MCP_HTTP_PATH` | `/mcp` | 对外 MCP 路径 |
| `MCP_API_KEY_VALIDATION_TTL_SECONDS` | `60` | Key 验证缓存 TTL |
| `MCP_API_KEY_VALIDATION_CACHE_MAX_ENTRIES` | `2048` | 最大缓存指纹数 |
| `MCP_API_KEY_VALIDATION_MAX_CONCURRENCY` | `16` | 最大并发验证数 |
| `WEKNORA_VERIFY_SSL` | `true` | 验证 WeKnora HTTPS 证书 |

请求级模式下应保持 `WEKNORA_API_KEY` 和 `MCP_SERVER_AUTH_TOKEN` 为空。

## HTTP 结果

| 状态码 | 含义 |
| --- | --- |
| `401` | 缺少 WeKnora API Key |
| `403` | Key 无效或不可用于 WeKnora API |
| `404` | 请求路径不在配置的 MCP 精确路径内 |
| `503` | WeKnora Key 验证暂时不可用或并发受限 |

## 本地测试

```bash
python -m pip install -e ".[test]"
pytest
```

测试覆盖缺失/无效 Key、外部 `X-API-Key` 拒绝、重叠请求身份隔离、
缓存不保存原始 Key、临时限流、Streamable HTTP 任务上下文继承和精确路径限制。

## 兼容模式

项目仍保留上游 `shared_secret` 模式，便于兼容旧客户端；多用户生产部署应使用
`MCP_AUTH_MODE=weknora_api_key`。

## 上游与许可证

本项目基于 WeKnora MCP Server 及其 MIT 许可代码改造。原始项目资料和兼容说明
保留在仓库文档中；本仓库的生产入口重点面向 WeKnora v0.7.1 多用户派发场景。
